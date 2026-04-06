import asyncio
import logging
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


async def crawl_thread_ids(
    config: Config,
    db: Database,
    client: HttpClient,
    *,
    progress_callback=None,
) -> dict:
    """Phase 1: Enumerate thread IDs, download page 1 of each."""
    stats = {"found": 0, "not_found": 0, "closed": 0, "errors": 0, "skipped": 0}

    # Determine start point (resume from last processed ID)
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
            await db.upsert_thread(thread_id, status=ThreadStatus.CLOSED)
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

    # Process IDs sequentially in batches
    batch_size = 50
    for batch_start in range(start_id, config.end_id + 1, batch_size):
        batch_end = min(batch_start + batch_size, config.end_id + 1)
        tasks = [process_id(tid) for tid in range(batch_start, batch_end)]
        await asyncio.gather(*tasks)

        # Save checkpoint
        await db.set_state("last_enumerated_id", str(batch_end - 1))

        if progress_callback:
            progress_callback(batch_end - 1, stats)

        # Check max requests
        if config.max_requests and client.request_count >= config.max_requests:
            logger.info(f"Max requests ({config.max_requests}) reached, stopping")
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
            page_count = thread["page_count"]
            pages_done = thread["pages_downloaded"]

            for pg in range(pages_done + 1, page_count + 1):
                # Check if page already downloaded
                existing = await db.get_page(thread_id, pg)
                if existing and existing["status"] == PageStatus.DOWNLOADED.value:
                    continue

                async with sem:
                    url = client.thread_url(thread_id, pg)
                    try:
                        result = await client.fetch(url)
                    except MaxRequestsReached:
                        return stats

                if result.error:
                    await db.upsert_page(
                        thread_id, pg, status=PageStatus.ERROR
                    )
                    stats["errors"] += 1
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
