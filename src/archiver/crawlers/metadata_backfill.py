"""Offline metadata backfill.

Recovers `forum_id`, `first_post_date` and `last_post_date` for already
downloaded `complete` threads by re-parsing their saved HTML. No network
access: everything comes from files on disk.

These signals are stable across the site's entire history (verified from
thread 1 / 1999 through the latest 2026 threads):

  * forum_id      -> the "log in to reply" link: login_dest=/msg/{fid}/{tid}
  * post datetime -> "Thread started MM/DD/YY h:mm(am|pm)" (first post)
                     "... posted MM/DD/YY h:mm(am|pm)"      (replies)
"""

import logging
import re
from pathlib import Path

from archiver.config import Config
from archiver.db import Database

logger = logging.getLogger(__name__)

_DT = re.compile(
    r"(?:Thread started|posted)\s+(\d{2})/(\d{2})/(\d{2})\s+(\d{1,2}):(\d{2})(am|pm)",
    re.IGNORECASE,
)


def extract_forum_id(html: bytes, thread_id: int) -> int | None:
    text = html.decode("utf-8", "ignore")
    for fid, tid in re.findall(r"login_dest=/msg/(\d+)/(\d+)", text):
        if int(tid) == thread_id:
            return int(fid)
    # Fallback: explicit forum post link present on most layouts.
    m = re.search(r"post\.php\?fid=(\d+)", text)
    return int(m.group(1)) if m else None


def _to_iso(mm: str, dd: str, yy: str, hh: str, mn: str, ap: str) -> str:
    yy_i = int(yy)
    year = 1900 + yy_i if yy_i >= 98 else 2000 + yy_i  # site started 1998
    hour = int(hh) % 12
    if ap.lower() == "pm":
        hour += 12
    return f"{year:04d}-{int(mm):02d}-{int(dd):02d} {hour:02d}:{int(mn):02d}"


def _datetimes(html: bytes) -> list[str]:
    text = html.decode("utf-8", "ignore")
    return [_to_iso(*m) for m in _DT.findall(text)]


def first_post_date(page1_html: bytes) -> str | None:
    dts = _datetimes(page1_html)
    return dts[0] if dts else None


def last_post_date(last_page_html: bytes) -> str | None:
    dts = _datetimes(last_page_html)
    return max(dts) if dts else None


async def backfill_metadata_from_html(
    config: Config,
    db: Database,
    *,
    do_forum: bool = True,
    do_dates: bool = True,
    progress_callback=None,
) -> dict:
    """Re-parse saved HTML to fill forum_id / post dates for complete threads."""
    stats = {"forum_id": 0, "dates": 0, "missing_files": 0, "no_match": 0}

    rows = await db.db.execute_fetchall(
        "SELECT thread_id, forum_id, first_post_date FROM threads WHERE status = 'complete'"
    )
    threads = [dict(r) for r in rows]
    logger.info(f"Backfilling metadata for {len(threads):,} complete threads")

    for i, t in enumerate(threads, 1):
        tid = t["thread_id"]
        need_forum = do_forum and t["forum_id"] is None
        need_dates = do_dates and not t["first_post_date"]
        if not need_forum and not need_dates:
            continue

        pages = await db.db.execute_fetchall(
            """SELECT page_num, html_path FROM thread_pages
               WHERE thread_id = ? AND status = 'downloaded'
               ORDER BY page_num""",
            (tid,),
        )
        if not pages:
            stats["missing_files"] += 1
            continue

        first_path = Path(pages[0]["html_path"])
        last_path = Path(pages[-1]["html_path"])
        if not first_path.exists():
            stats["missing_files"] += 1
            continue

        page1 = first_path.read_bytes()
        forum_id = first_post = last_post = None

        if need_forum:
            forum_id = extract_forum_id(page1, tid)
        if need_dates:
            first_post = first_post_date(page1)
            last_html = (
                last_path.read_bytes()
                if last_path != first_path and last_path.exists()
                else page1
            )
            last_post = last_post_date(last_html)

        if forum_id is None and first_post is None and last_post is None:
            stats["no_match"] += 1
            continue

        await db.update_thread_metadata(
            tid,
            forum_id=forum_id,
            first_post_date=first_post,
            last_post_date=last_post,
        )
        if forum_id is not None:
            stats["forum_id"] += 1
        if first_post or last_post:
            stats["dates"] += 1

        if progress_callback and i % 5000 == 0:
            progress_callback(i, len(threads), stats)

    return stats
