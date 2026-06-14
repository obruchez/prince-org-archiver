"""Integrate Wayback-recovered HTML into the main thread schema.

The Wayback recovery crawler dumps snapshots into `data/wayback/threads/`
and records provenance in the `wayback` table, but it never touches the
canonical `threads` / `thread_pages` tables. That makes the recovered
content invisible to anything that queries the live archive (titles,
authors, page counts, media references).

This module reads each recovered Wayback row, parses it with the same
thread-page parser the live crawl uses, then:

  * enriches the `threads` row with title/author/page_count (without
    touching `status`, which stays 'closed' or 'complete' to record what
    the *live* site said);
  * writes a `thread_pages` row at page_num=1 with `source='wayback'`
    and the snapshot timestamp (replaces the useless 'error' stub row
    for gated threads; creates a new row for closed threads);
  * registers any media URLs the snapshot references so the media
    crawler can pull avatars/post images that only existed on the
    closed-forum threads.

Idempotent: running it again on already-integrated threads is a no-op
in effect (COALESCE preserves filled fields; thread_pages upsert is the
same as the first run).
"""

import logging
from pathlib import Path

from archiver.config import Config
from archiver.db import Database
from archiver.models import ResponseType
from archiver.parsers.thread_page import parse_thread_page
from archiver.storage.media_writer import classify_media_url

logger = logging.getLogger(__name__)


async def integrate_recovered_wayback(
    config: Config,
    db: Database,
    *,
    limit: int | None = None,
    progress_callback=None,
) -> dict:
    """Walk every recovered Wayback thread row and fold its content into
    the main schema. Returns counters; does not raise on per-row errors."""
    stats = {
        "scanned": 0,
        "enriched": 0,    # threads row updated with parsed metadata
        "unparseable": 0, # HTML present but parser didn't classify as a thread
        "missing_file": 0, # DB says html_path but file is gone
        "media_added": 0,
    }

    rows = await db.get_recovered_wayback_threads(limit=limit)
    if not rows:
        return stats

    for row in rows:
        stats["scanned"] += 1
        thread_id = row["thread_id"]
        html_path = row["html_path"]
        snapshot_ts = row["snapshot_ts"]

        p = Path(html_path)
        if not p.exists():
            logger.warning(f"wayback {thread_id}: html_path missing on disk: {p}")
            stats["missing_file"] += 1
            continue

        try:
            html_bytes = p.read_bytes()
        except OSError as e:
            logger.warning(f"wayback {thread_id}: cannot read {p}: {e}")
            stats["missing_file"] += 1
            continue

        parsed = parse_thread_page(thread_id, 1, html_bytes)
        if parsed.response_type != ResponseType.THREAD_FOUND or not parsed.metadata:
            # Snapshot is on disk but doesn't look like a thread page --
            # surprising for a 'recovered' row (the wayback crawler's
            # _looks_like_thread should have rejected it), but the parser
            # is stricter, so we just skip rather than poison the row.
            logger.debug(
                f"wayback {thread_id}: parser saw "
                f"{parsed.response_type.value}, skipping enrichment"
            )
            stats["unparseable"] += 1
            continue

        meta = parsed.metadata
        await db.enrich_thread_from_wayback(
            thread_id,
            forum_id=meta.forum_id,
            title=meta.title,
            author=meta.author,
            page_count=meta.page_count,
        )
        await db.upsert_wayback_page(
            thread_id, 1,
            html_path=html_path,
            snapshot_ts=snapshot_ts,
            post_count=parsed.post_count,
            file_size=len(html_bytes),
        )

        for url in parsed.media_urls:
            media_type = classify_media_url(url)
            if media_type:
                await db.add_media(url, media_type, source_thread_id=thread_id)
                stats["media_added"] += 1

        stats["enriched"] += 1
        if progress_callback and stats["scanned"] % 200 == 0:
            progress_callback(stats)

    return stats
