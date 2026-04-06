import asyncio
import logging

from archiver.client import HttpClient, MaxRequestsReached
from archiver.config import Config
from archiver.db import Database
from archiver.storage.html_writer import save_events_page

logger = logging.getLogger(__name__)

# Site established in 1998
START_YEAR = 1998
START_MONTH = 1


async def crawl_events(
    config: Config,
    db: Database,
    client: HttpClient,
    *,
    end_year: int = 2026,
    end_month: int = 12,
    progress_callback=None,
) -> dict:
    """Archive events calendar pages."""
    stats = {"downloaded": 0, "errors": 0, "skipped": 0}

    sem = asyncio.Semaphore(config.concurrency)

    year = START_YEAR
    month = START_MONTH

    while (year, month) <= (end_year, end_month):
        # Check if already downloaded
        existing = await db.db.execute_fetchall(
            "SELECT status FROM events WHERE year = ? AND month = ?",
            (year, month),
        )
        if existing and existing[0][0] == "downloaded":
            stats["skipped"] += 1
            month += 1
            if month > 12:
                month = 1
                year += 1
            continue

        async with sem:
            url = client.events_url(year, month)
            try:
                result = await client.fetch(url)
            except MaxRequestsReached:
                return stats

        if result.error or result.status_code != 200:
            await db.upsert_event(year, month, status="error")
            stats["errors"] += 1
            logger.warning(f"Events {year}/{month}: error")
        else:
            html_path = save_events_page(config, year, month, result.content)
            await db.upsert_event(
                year, month, status="downloaded", html_path=str(html_path)
            )
            stats["downloaded"] += 1
            logger.debug(f"Events {year}/{month}: saved")

        if progress_callback:
            progress_callback(year, month, stats)

        month += 1
        if month > 12:
            month = 1
            year += 1

        if config.max_requests and client.request_count >= config.max_requests:
            break

    return stats
