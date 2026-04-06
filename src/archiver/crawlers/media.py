import asyncio
import logging

from archiver.client import HttpClient, MaxRequestsReached
from archiver.config import Config
from archiver.db import Database
from archiver.models import MediaStatus, MediaType
from archiver.storage.media_writer import save_media

logger = logging.getLogger(__name__)


async def crawl_media(
    config: Config,
    db: Database,
    client: HttpClient,
    *,
    media_type: MediaType | None = None,
    progress_callback=None,
) -> dict:
    """Download pending media files."""
    stats = {"downloaded": 0, "errors": 0, "skipped": 0}
    sem = asyncio.Semaphore(config.concurrency)

    while True:
        pending = await db.get_pending_media(media_type=media_type, limit=50)
        if not pending:
            break

        async def download(item: dict) -> None:
            url = item["url"]
            mtype = MediaType(item["type"])

            async with sem:
                try:
                    result = await client.fetch(url)
                except MaxRequestsReached:
                    return

            if result.error or result.status_code != 200:
                await db.update_media_status(
                    url,
                    MediaStatus.ERROR,
                    error_message=result.error or f"HTTP {result.status_code}",
                )
                stats["errors"] += 1
                return

            if len(result.content) == 0:
                await db.update_media_status(
                    url, MediaStatus.SKIPPED, error_message="Empty response"
                )
                stats["skipped"] += 1
                return

            path = save_media(config, url, mtype, result.content)
            await db.update_media_status(
                url,
                MediaStatus.DOWNLOADED,
                local_path=str(path),
                file_size=len(result.content),
            )
            stats["downloaded"] += 1

        tasks = [download(item) for item in pending]
        await asyncio.gather(*tasks)

        if progress_callback:
            progress_callback(stats)

        if config.max_requests and client.request_count >= config.max_requests:
            break

    return stats
