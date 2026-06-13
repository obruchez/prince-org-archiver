"""Wayback Machine (archive.org) recovery crawler.

Salvages content that is gone from the live site:

  * gated stubs  -> threads moderator-removed since our crawl (saved as
                    the 3.5 KB error page; DB forum_id IS NULL)
  * closed       -> threads in permanently closed forums
  * index        -> forum listing pages (heavily archived; recovers
                    titles/links/first posts even when thread pages
                    were never snapshotted)

Two-step per target: CDX API lists snapshots, then the raw page is
fetched via the `id_` replay modifier (original bytes, no IA toolbar).
Coverage is partial and skewed toward older content -- recent removed
threads frequently have no captures at all.

archive.org is rate-sensitive (we saw 503/504 under light load), so
this crawler is deliberately slow, single-flight, and backs off hard.
It is fully resumable via the `wayback` table.
"""

import asyncio
import json
import logging
import time
import zlib

import httpx

from archiver.config import Config
from archiver.crawlers.metadata_backfill import extract_forum_id
from archiver.db import Database
from archiver.storage.html_writer import (
    save_wayback_index,
    save_wayback_thread,
    thread_dir,
)

logger = logging.getLogger(__name__)

CDX_URL = "https://web.archive.org/cdx/search/cdx"
REPLAY = "https://web.archive.org/web/{ts}id_/{url}"

# Per target, fetch at most this many candidate snapshots (oldest first)
# before giving up -- bounds requests on low-yield targets.
MAX_SNAPSHOTS_PER_TARGET = 6
MAX_BACKOFF = 120.0
# Cap adaptive pacing at one request every 10s. archive.org's
# (undocumented) CDX limit is roughly 15 req/min for unauthenticated
# clients; this stays comfortably under that even after we've grown.
MAX_PACE = 10.0
# Bail out of the run after this many targets in a row come back
# throttled -- archive.org clearly wants us gone for now; resume later.
MAX_CONSECUTIVE_THROTTLES = 5


class WaybackThrottled(Exception):
    """archive.org persistently rate-limited the request. Distinct from
    `client.get()` returning None (which means a non-retryable miss like
    404) so callers can keep the target `pending` instead of burning it
    as `no_capture` or `error`."""


class WaybackClient:
    """Gentle single-flight client: adaptive spacing + hard backoff on the
    statuses archive.org throws when it wants you to slow down.

    Pacing grows (never shrinks) on every throttle response, so a session
    that trips the rate limit settles at a slower pace for the rest of
    its life instead of oscillating between fast and 429'd."""

    def __init__(self, rate: float):
        self._min_interval = 1.0 / rate if rate > 0 else 1.0
        self._last = 0.0
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self):
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(90.0),
            follow_redirects=True,
            headers={"User-Agent": "PrinceOrgArchiver/0.1 (archival research)"},
        )
        return self

    async def __aexit__(self, *a):
        if self._client:
            await self._client.aclose()

    async def _pace(self) -> None:
        wait = self._min_interval - (time.monotonic() - self._last)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last = time.monotonic()

    def _grow_pace(self) -> None:
        new = min(self._min_interval * 2, MAX_PACE)
        if new > self._min_interval:
            logger.info(
                f"wayback: pace -> 1 req / {new:.1f}s "
                f"(was {self._min_interval:.1f}s)"
            )
            self._min_interval = new

    async def get(self, url: str, params: dict | None = None) -> httpx.Response | None:
        """Returns a 200 response, or None for a non-retryable miss (404
        or a body archive.org served with an unreadable Content-Encoding
        even after asking for raw bytes). Raises WaybackThrottled when 6
        attempts of 429/5xx/timeout are exhausted -- the caller must
        keep the target pending in that case, not mark it no_capture."""
        backoff = 5.0
        throttled = False
        # httpx eagerly reads + decompresses the body inside the get()
        # call, so a body with a mislabeled Content-Encoding (zlib's
        # "incorrect header check") raises here, BEFORE callers get to
        # touch r.content. After one such failure we retry asking the
        # server to skip compression entirely; that recovers most of
        # archive.org's mis-encoded snapshots.
        no_encoding = False
        for attempt in range(6):
            await self._pace()
            hdrs = {"Accept-Encoding": "identity"} if no_encoding else None
            try:
                r = await self._client.get(url, params=params, headers=hdrs)
                _ = r.content  # force decode here if get() didn't already
            except (httpx.TimeoutException, httpx.TransportError) as e:
                logger.debug(f"wayback transport error: {e}")
                throttled = True
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
                continue
            except (httpx.DecodingError, zlib.error, OSError, ValueError) as e:
                if not no_encoding:
                    logger.info(
                        f"wayback decode failed on {url}; "
                        f"retrying once with Accept-Encoding: identity ({e})"
                    )
                    no_encoding = True
                    continue
                logger.debug(
                    f"wayback decode failed even with identity: {e}"
                )
                return None
            if r.status_code == 200:
                return r
            if r.status_code in (429, 503, 504, 502):
                throttled = True
                self._grow_pace()
                retry_after = r.headers.get("Retry-After")
                delay = (
                    float(retry_after)
                    if retry_after and retry_after.isdigit()
                    else backoff
                )
                logger.warning(
                    f"archive.org {r.status_code}; backing off {delay:.0f}s"
                )
                await asyncio.sleep(delay)
                backoff = min(backoff * 2, MAX_BACKOFF)
                continue
            # 404 etc. -- nothing here, don't retry.
            return None
        if throttled:
            raise WaybackThrottled(
                f"6 attempts exhausted (last backoff {backoff:.0f}s)"
            )
        return None


def _looks_like_thread(html: bytes) -> bool:
    return (
        b"Thread started" in html
        and b"<title>error</title>" not in html
        and b"not yet approved" not in html
    )


def _looks_like_index(html: bytes) -> bool:
    return b"/msg/" in html and b"<title>error</title>" not in html


async def _cdx(client: WaybackClient, params: dict) -> list[list[str]]:
    """Return CDX rows (without the header row); [] on no captures/failure."""
    p = {
        "output": "json",
        "fl": "timestamp,original,statuscode,digest,length",
        "collapse": "digest",
        "filter": "statuscode:200",
        "limit": "40",
        **params,
    }
    r = await client.get(CDX_URL, params=p)
    if r is None:
        return []
    try:
        txt = r.text.strip()
    except (httpx.DecodingError, zlib.error, OSError, ValueError) as e:
        # archive.org occasionally returns a CDX body with a mislabeled
        # Content-Encoding zlib refuses. Treat as "no captures" (the _cdx
        # failure contract) rather than erroring the whole target.
        logger.debug(f"wayback CDX undecodable: {e}")
        return []
    if not txt or txt == "[]":
        return []
    try:
        rows = json.loads(txt)
    except json.JSONDecodeError:
        return []
    return rows[1:] if rows else []


def _thread_cdx_params(thread_id: int, forum_id: int) -> dict:
    # ONLY tight prefix queries. A domain-wide CDX scan with a server-side
    # regex filter makes archive.org 504 every time (it burned an entire
    # overnight run via the backoff ladder), so we never do that: a target
    # whose forum_id we cannot determine cheaply is skipped, not scanned.
    # Prefix match still catches ?pg=N, scheme/host variants, :80, etc.
    return {"url": f"prince.org/msg/{forum_id}/{thread_id}", "matchType": "prefix"}


async def _recover_thread(
    client: WaybackClient, config: Config, db: Database, row: dict
) -> str:
    thread_id = int(row["target_key"])
    forum_id = row["forum_id"]
    if forum_id is None:
        # Try to learn the forum from the saved stub's chrome (gated stubs
        # always have this; closed-forum threads usually do not).
        stub = thread_dir(config, thread_id) / "page_1.html"
        if stub.exists():
            forum_id = extract_forum_id(stub.read_bytes(), thread_id)

    if forum_id is None:
        # Defensive: the worklist row may carry a stale NULL (seeded before
        # `crawl backfill` filled it). sync_wayback_forum_ids() normally
        # reconciles this up front, but fall back to the live threads row
        # so a stale worklist can never silently waste a backfill.
        t = await db.get_thread(thread_id)
        if t:
            forum_id = t["forum_id"]

    if forum_id is None:
        # No cheap way to know the forum -> do NOT domain-scan. Defer it;
        # `crawl backfill` can fill closed-thread forum_ids, after which
        # re-running wayback requeues these automatically.
        await db.update_wayback(
            "thread", str(thread_id), status="no_forum", snapshots_found=0,
            error_message="forum_id unknown; run 'crawl backfill' then retry",
        )
        return "no_forum"

    snaps = await _cdx(client, _thread_cdx_params(thread_id, forum_id))
    if not snaps:
        await db.update_wayback(
            "thread", str(thread_id), status="no_capture", snapshots_found=0
        )
        return "no_capture"

    snaps.sort(key=lambda r: r[0])  # oldest first: most likely pre-removal
    for ts, original, *_ in snaps[:MAX_SNAPSHOTS_PER_TARGET]:
        r = await client.get(REPLAY.format(ts=ts, url=original))
        if r is None:
            continue
        try:
            html = r.content
        except (httpx.DecodingError, zlib.error, OSError, ValueError) as e:
            # Some old id_ captures carry a mislabeled / doubly-applied
            # Content-Encoding that zlib refuses ("incorrect header
            # check"). Skip just this snapshot and try the next one
            # rather than failing the whole thread.
            logger.debug(f"wayback {thread_id} snapshot {ts} undecodable: {e}")
            continue
        if _looks_like_thread(html):
            path = save_wayback_thread(config, thread_id, ts, html)
            await db.update_wayback(
                "thread", str(thread_id), status="recovered",
                snapshot_ts=ts, html_path=str(path),
                snapshots_found=len(snaps),
            )
            return "recovered"

    await db.update_wayback(
        "thread", str(thread_id), status="no_capture",
        snapshots_found=len(snaps),
        error_message="snapshots found but none showed thread content",
    )
    return "no_capture"


async def _recover_index(
    client: WaybackClient, config: Config, db: Database, row: dict
) -> str:
    forum_id = int(row["target_key"])
    snaps = await _cdx(
        client, {"url": f"prince.org/msg/{forum_id}", "matchType": "prefix"}
    )
    if not snaps:
        await db.update_wayback(
            "index", str(forum_id), status="no_capture", snapshots_found=0
        )
        return "no_capture"

    # Keep one snapshot per calendar year to capture the forum's evolution
    # without re-downloading near-identical daily crawls.
    by_year: dict[str, list[str]] = {}
    for ts, original, *_ in sorted(snaps, key=lambda r: r[0]):
        by_year.setdefault(ts[:4], [original, ts])

    saved = 0
    last_ts = None
    for year, (original, ts) in sorted(by_year.items()):
        r = await client.get(REPLAY.format(ts=ts, url=original))
        if r is None or not _looks_like_index(r.content):
            continue
        save_wayback_index(config, forum_id, ts, r.content)
        saved += 1
        last_ts = ts

    status = "recovered" if saved else "no_capture"
    await db.update_wayback(
        "index", str(forum_id), status=status,
        snapshot_ts=last_ts, snapshots_found=len(snaps),
        error_message=None if saved else "snapshots found but unusable",
    )
    return status


async def recover_via_wayback(
    config: Config,
    db: Database,
    *,
    targets: list[str],
    forum_ids: list[int] | None = None,
    limit: int | None = None,
    progress_callback=None,
) -> dict:
    """Seed the worklist for the requested targets and work it.

    targets: any of 'gated', 'closed', 'index'.
    """
    if "gated" in targets:
        await db.seed_wayback_threads("gated")
    if "closed" in targets:
        await db.seed_wayback_threads("closed")
    if "gated" in targets or "closed" in targets:
        # Pull in forum_ids resolved since the worklist was seeded (e.g. by
        # `crawl backfill`) and rescue targets deferred as no_forum. Pending
        # thread rows are worked regardless of which thread target is asked
        # for, so always reconcile when any thread target is in play.
        requeued = await db.sync_wayback_forum_ids()
        if requeued:
            logger.info(f"Requeued {requeued} wayback targets (forum_id now known)")
    if "index" in targets:
        await db.seed_wayback_index(forum_ids or sorted(
            {7, 100, 8, 5, 5001, 9, 12, 3, 2, 13}
        ))

    want_thread = "gated" in targets or "closed" in targets
    want_index = "index" in targets

    stats = {"recovered": 0, "no_capture": 0, "error": 0, "throttled": 0,
             "done": 0}
    consecutive_throttles = 0

    async with WaybackClient(config.wayback_rate) as client:
        while True:
            if limit is not None and stats["done"] >= limit:
                break

            batch: list[dict] = []
            if want_index:
                batch += await db.get_pending_wayback("index", limit=20)
            if want_thread and len(batch) < 50:
                batch += await db.get_pending_wayback(
                    "thread", limit=50 - len(batch)
                )
            if not batch:
                break

            stop_run = False
            for row in batch:
                if limit is not None and stats["done"] >= limit:
                    break
                try:
                    if row["target_type"] == "index":
                        outcome = await _recover_index(client, config, db, row)
                    else:
                        outcome = await _recover_thread(client, config, db, row)
                    consecutive_throttles = 0
                except WaybackThrottled as e:
                    # Don't burn the DB row -- leave it pending so the next
                    # run can retry. Just count it locally.
                    logger.warning(
                        f"wayback throttled on {row['target_key']}: {e}; "
                        "leaving pending"
                    )
                    outcome = "throttled"
                    consecutive_throttles += 1
                except Exception as e:  # never let one bad target stop the run
                    logger.exception(f"wayback target {row['target_key']} failed")
                    await db.update_wayback(
                        row["target_type"], row["target_key"],
                        status="error", error_message=str(e)[:200],
                    )
                    outcome = "error"
                    consecutive_throttles = 0

                stats[outcome] = stats.get(outcome, 0) + 1
                stats["done"] += 1
                if progress_callback and stats["done"] % 25 == 0:
                    progress_callback(stats)

                if consecutive_throttles >= MAX_CONSECUTIVE_THROTTLES:
                    logger.warning(
                        f"wayback: {consecutive_throttles} throttles in a "
                        "row -- archive.org wants us gone; stopping, "
                        "resume later"
                    )
                    stop_run = True
                    break

            if stop_run:
                break

    return stats
