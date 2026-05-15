import asyncio
import logging

from archiver.client import HttpClient, MaxRequestsReached
from archiver.config import Config
from archiver.crawlers.threads import _forum_id_from_url
from archiver.db import Database

logger = logging.getLogger(__name__)


async def backfill_closed_forum_ids(
    config: Config,
    db: Database,
    client: HttpClient,
    *,
    progress_callback=None,
) -> dict:
    """Re-fetch closed threads that are missing a forum_id to fill it in."""
    stats = {"updated": 0, "errors": 0}
    sem = asyncio.Semaphore(config.concurrency)

    while True:
        threads = await db.get_closed_threads_missing_forum(limit=50)
        if not threads:
            break

        for thread in threads:
            thread_id = thread["thread_id"]

            async with sem:
                url = client.thread_url(thread_id)
                try:
                    result = await client.fetch(url)
                except MaxRequestsReached:
                    return stats

            if result.error:
                stats["errors"] += 1
                continue

            forum_id = _forum_id_from_url(result.final_url)
            if forum_id:
                await db.update_thread_forum(thread_id, forum_id)
                stats["updated"] += 1
            else:
                stats["errors"] += 1

        if progress_callback:
            progress_callback(stats)

        if config.max_requests and client.request_count >= config.max_requests:
            break

    return stats
