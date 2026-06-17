import asyncio
import logging
import re
import time

from archiver.client import HttpClient, MaxRequestsReached
from archiver.config import Config
from archiver.db import Database
from archiver.models import (
    MediaType,
    PageStatus,
    ResponseType,
    ThreadStatus,
)
from archiver.parsers.thread_page import parse_thread_page
from archiver.storage.html_writer import save_thread_metadata, save_thread_page
from archiver.storage.media_writer import classify_media_url

logger = logging.getLogger(__name__)

# Per-page fetch attempts before the page is marked terminally 'failed'.
# This is what guarantees the Phase 2 loop can finish: a page that can
# never be fetched stops blocking termination after this many tries.
MAX_PAGE_RETRIES = 5


def _forum_id_from_url(url: str | None) -> int | None:
    """Extract forum ID from a URL like https://prince.org/msg/105/12345."""
    if not url:
        return None
    match = re.search(r"/msg/(\d+)/", url)
    return int(match.group(1)) if match else None


async def crawl_thread_ids(
    config: Config,
    db: Database,
    client: HttpClient,
    *,
    progress_callback=None,
) -> dict:
    """Phase 1: Enumerate thread IDs, download page 1 of each."""
    stats = {"found": 0, "not_found": 0, "closed": 0, "errors": 0, "skipped": 0}

    # Determine start point. An explicit --start-id (anything other than
    # the default 1) overrides the resume checkpoint, so specific ID ranges
    # can be re-processed (e.g. retrying errored threads). Otherwise resume
    # from the last enumerated ID.
    if config.start_id != 1:
        start_id = config.start_id
    else:
        last_id = await db.get_state("last_enumerated_id")
        start_id = int(last_id) + 1 if last_id else config.start_id

    logger.info(f"Starting thread enumeration from ID {start_id} to {config.end_id}")

    sem = asyncio.Semaphore(config.concurrency)

    async def process_id(thread_id: int) -> None:
        # Check if already processed
        existing = await db.get_thread(thread_id)
        if existing and existing["status"] != ThreadStatus.PENDING.value:
            if existing["status"] != ThreadStatus.ERROR.value or not config.retry_errors:
                stats["skipped"] += 1
                return

        async with sem:
            url = client.thread_url(thread_id)
            try:
                result = await client.fetch(url)
            except MaxRequestsReached:
                return

        if result.error:
            await db.upsert_thread(
                thread_id, status=ThreadStatus.ERROR, error_message=result.error
            )
            stats["errors"] += 1
            logger.debug(f"Thread {thread_id}: error - {result.error}")
            return

        if result.status_code == 404:
            await db.upsert_thread(thread_id, status=ThreadStatus.NOT_FOUND)
            stats["not_found"] += 1
            return

        if result.status_code == 403:
            await db.upsert_thread(
                thread_id, status=ThreadStatus.ERROR,
                error_message="HTTP 403 Forbidden",
            )
            stats["errors"] += 1
            return

        if result.status_code not in (200, 301, 302):
            await db.upsert_thread(
                thread_id, status=ThreadStatus.ERROR,
                error_message=f"HTTP {result.status_code}",
            )
            stats["errors"] += 1
            return

        parsed = parse_thread_page(thread_id, 1, result.content)

        if parsed.response_type == ResponseType.NOT_FOUND:
            await db.upsert_thread(thread_id, status=ThreadStatus.NOT_FOUND)
            stats["not_found"] += 1
            return

        if parsed.response_type == ResponseType.FORUM_CLOSED:
            # Extract forum ID from redirect URL (e.g. /msg/105/12345 -> forum 105)
            forum_id = _forum_id_from_url(result.final_url)
            await db.upsert_thread(thread_id, forum_id=forum_id, status=ThreadStatus.CLOSED)
            stats["closed"] += 1
            return

        # Thread found - save HTML and metadata
        meta = parsed.metadata
        if not meta:
            await db.upsert_thread(thread_id, status=ThreadStatus.ERROR, error_message="No metadata parsed")
            stats["errors"] += 1
            return

        # Apply forum filter if set
        if config.forum_filter and meta.forum_id != config.forum_filter:
            # Still save the thread info but don't download content
            await db.upsert_thread(
                thread_id,
                forum_id=meta.forum_id,
                title=meta.title,
                author=meta.author,
                page_count=meta.page_count,
                status=ThreadStatus.COMPLETE,
            )
            stats["skipped"] += 1
            return

        # Save page 1 HTML
        html_path = save_thread_page(config, thread_id, 1, result.content)

        # Save metadata
        save_thread_metadata(config, meta)

        # Update DB
        await db.upsert_thread(
            thread_id,
            forum_id=meta.forum_id,
            title=meta.title,
            author=meta.author,
            page_count=meta.page_count,
            status=ThreadStatus.COMPLETE,
        )
        await db.upsert_page(
            thread_id,
            1,
            status=PageStatus.DOWNLOADED,
            html_path=str(html_path),
            post_count=parsed.post_count,
            file_size=len(result.content),
        )
        await db.increment_pages_downloaded(thread_id)

        # Queue remaining pages
        for pg in range(2, meta.page_count + 1):
            await db.upsert_page(thread_id, pg, status=PageStatus.PENDING)

        # Register media URLs
        for url in parsed.media_urls:
            media_type = classify_media_url(url)
            if media_type:
                await db.add_media(url, media_type, source_thread_id=thread_id)

        stats["found"] += 1
        if meta.page_count > 1:
            logger.info(
                f"Thread {thread_id}: '{meta.title}' "
                f"(forum {meta.forum_id}, {meta.page_count} pages)"
            )

    # A targeted re-run (explicit --start-id) must not rewind the global
    # enumeration high-water mark used by normal resume.
    track_checkpoint = config.start_id == 1

    # Process IDs sequentially in batches
    batch_size = 50
    for batch_start in range(start_id, config.end_id + 1, batch_size):
        batch_end = min(batch_start + batch_size, config.end_id + 1)
        tasks = [process_id(tid) for tid in range(batch_start, batch_end)]
        await asyncio.gather(*tasks)

        # Save checkpoint
        if track_checkpoint:
            await db.set_state("last_enumerated_id", str(batch_end - 1))

        if progress_callback:
            progress_callback(batch_end - 1, stats)

        # Check max requests
        if config.max_requests and client.request_count >= config.max_requests:
            logger.info(f"Max requests ({config.max_requests}) reached, stopping")
            break

    return stats


async def refresh_recent_threads(
    config: Config,
    db: Database,
    client: HttpClient,
    *,
    since_date: str,
    forum_ids: list[int] | None = None,
    progress_callback=None,
) -> dict:
    """Re-pull threads whose last_post_date is on/after `since_date` so the
    archive picks up new replies.

    Strategy is partial-refresh rather than full re-download:
      - Page 1 is always re-fetched (cheap, reveals the current page_count
        and any title/author changes).
      - The previously-last page is re-queued -- on this forum, new posts
        land on the existing last page until it fills up, after which a
        new page is created. Re-fetching the old last page is the only
        way to capture those in-place additions.
      - Pages above the old page_count are queued as PENDING.
      - Pages strictly between page 2 and old_last_page - 1 are NOT
        re-queued; on this forum's UI they're frozen the moment they
        spill over to the next page.

    Phase 2 (`crawl_remaining_pages`) then handles the actual downloads
    for everything we queued, so the typical workflow is to call this
    function and let the normal crawl pipeline continue.
    """
    stats = {
        "checked": 0, "grew": 0, "unchanged": 0,
        "pages_queued": 0, "errors": 0,
    }

    targets = await db.get_threads_for_refresh(
        since_date=since_date, forum_ids=forum_ids
    )
    if not targets:
        logger.info(f"No threads with activity since {since_date} to refresh")
        return stats

    logger.info(
        f"Refreshing {len(targets):,} threads with activity since {since_date}"
        + (f" (forums {forum_ids})" if forum_ids else "")
    )

    sem = asyncio.Semaphore(config.concurrency)

    async def refresh_one(row: dict) -> None:
        thread_id = row["thread_id"]
        old_page_count = row["page_count"] or 1
        stats["checked"] += 1

        async with sem:
            url = client.thread_url(thread_id)
            try:
                result = await client.fetch(url)
            except MaxRequestsReached:
                return

        if result.error or result.status_code not in (200, 301, 302):
            stats["errors"] += 1
            logger.debug(
                f"Thread {thread_id}: refresh failed "
                f"({result.error or 'HTTP ' + str(result.status_code)})"
            )
            return

        parsed = parse_thread_page(thread_id, 1, result.content)
        meta = parsed.metadata
        if parsed.response_type != ResponseType.SUCCESS or not meta:
            # Thread went private/got closed/got removed since last crawl.
            # Don't downgrade the existing row's status -- the historical
            # capture on disk is still valid -- just count and move on.
            stats["errors"] += 1
            logger.info(
                f"Thread {thread_id}: now {parsed.response_type.value}, "
                "leaving existing archive row intact"
            )
            return

        # Save the fresh page 1
        html_path = save_thread_page(config, thread_id, 1, result.content)
        save_thread_metadata(config, meta)

        await db.upsert_thread(
            thread_id,
            forum_id=meta.forum_id,
            title=meta.title,
            author=meta.author,
            page_count=meta.page_count,
            status=ThreadStatus.COMPLETE,
        )
        await db.upsert_page(
            thread_id, 1,
            status=PageStatus.DOWNLOADED,
            html_path=str(html_path),
            post_count=parsed.post_count,
            file_size=len(result.content),
        )

        for media_url in parsed.media_urls:
            mtype = classify_media_url(media_url)
            if mtype:
                await db.add_media(media_url, mtype, source_thread_id=thread_id)

        # Re-queue the previously-last page (where new replies land in place)
        # plus any newly-created pages above old_page_count.
        # max(2, ...) skips page 1 (we just downloaded it).
        queue_from = max(2, old_page_count)
        queued_here = 0
        for pg in range(queue_from, meta.page_count + 1):
            await db.upsert_page(thread_id, pg, status=PageStatus.PENDING)
            queued_here += 1

        stats["pages_queued"] += queued_here
        if meta.page_count > old_page_count:
            stats["grew"] += 1
        else:
            stats["unchanged"] += 1

        if progress_callback:
            progress_callback(stats)

    # Drive in batches so the progress callback fires periodically and the
    # rate limiter doesn't have to chew through tens of thousands of tasks
    # queued at once.
    batch_size = 50
    for i in range(0, len(targets), batch_size):
        batch = targets[i : i + batch_size]
        await asyncio.gather(*(refresh_one(r) for r in batch))
        if config.max_requests and client.request_count >= config.max_requests:
            logger.info(f"Max requests ({config.max_requests}) reached, stopping refresh")
            break

    return stats


async def crawl_remaining_pages(
    config: Config,
    db: Database,
    client: HttpClient,
    *,
    progress_callback=None,
) -> dict:
    """Phase 2: Download remaining pages of multi-page threads."""
    stats = {"downloaded": 0, "errors": 0}

    sem = asyncio.Semaphore(config.concurrency)

    while True:
        # Get threads that need more pages, prioritized by forum
        threads = await db.get_threads_needing_pages(
            forum_ids=config.priority_forums, limit=50
        )
        if not threads:
            # Try without forum filter
            threads = await db.get_threads_needing_pages(limit=50)
        if not threads:
            break

        for thread in threads:
            thread_id = thread["thread_id"]

            for pg in await db.get_pending_pages(thread_id):
                async with sem:
                    url = client.thread_url(thread_id, pg)
                    try:
                        result = await client.fetch(url)
                    except MaxRequestsReached:
                        return stats

                if result.error:
                    final_status = await db.record_page_failure(
                        thread_id, pg, MAX_PAGE_RETRIES
                    )
                    stats["errors"] += 1
                    if final_status == PageStatus.FAILED.value:
                        logger.warning(
                            f"Thread {thread_id} page {pg}: giving up after "
                            f"{MAX_PAGE_RETRIES} attempts ({result.error})"
                        )
                    continue

                # Save HTML
                html_path = save_thread_page(config, thread_id, pg, result.content)

                # Parse for media URLs
                parsed = parse_thread_page(thread_id, pg, result.content)
                for url in parsed.media_urls:
                    media_type = classify_media_url(url)
                    if media_type:
                        await db.add_media(url, media_type, source_thread_id=thread_id)

                await db.upsert_page(
                    thread_id,
                    pg,
                    status=PageStatus.DOWNLOADED,
                    html_path=str(html_path),
                    post_count=parsed.post_count,
                    file_size=len(result.content),
                )
                await db.increment_pages_downloaded(thread_id)
                stats["downloaded"] += 1

                if progress_callback:
                    progress_callback(thread_id, pg, stats)

                if config.max_requests and client.request_count >= config.max_requests:
                    return stats

    return stats
