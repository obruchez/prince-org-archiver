import json
import logging
from pathlib import Path

import aiosqlite

from archiver.models import MediaStatus, MediaType, PageStatus, ThreadStatus

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS forums (
    forum_id    INTEGER PRIMARY KEY,
    name        TEXT,
    thread_count INTEGER,
    reply_count  INTEGER,
    status      TEXT DEFAULT 'active',
    discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_crawled TIMESTAMP
);

CREATE TABLE IF NOT EXISTS threads (
    thread_id    INTEGER PRIMARY KEY,
    forum_id     INTEGER,
    title        TEXT,
    author       TEXT,
    reply_count  INTEGER,
    view_count   INTEGER,
    page_count   INTEGER DEFAULT 1,
    first_post_date TEXT,
    last_post_date  TEXT,
    status       TEXT DEFAULT 'pending',
    pages_downloaded INTEGER DEFAULT 0,
    discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_crawled  TIMESTAMP,
    error_message TEXT,
    retry_count   INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS thread_pages (
    thread_id   INTEGER NOT NULL,
    page_num    INTEGER NOT NULL,
    status      TEXT DEFAULT 'pending',
    html_path   TEXT,
    post_count  INTEGER,
    downloaded_at TIMESTAMP,
    file_size   INTEGER,
    PRIMARY KEY (thread_id, page_num)
);

CREATE TABLE IF NOT EXISTS media (
    url          TEXT PRIMARY KEY,
    type         TEXT NOT NULL,
    local_path   TEXT,
    status       TEXT DEFAULT 'pending',
    file_size    INTEGER,
    content_type TEXT,
    source_thread_id INTEGER,
    discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    downloaded_at TIMESTAMP,
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS events (
    year        INTEGER NOT NULL,
    month       INTEGER NOT NULL,
    status      TEXT DEFAULT 'pending',
    html_path   TEXT,
    downloaded_at TIMESTAMP,
    PRIMARY KEY (year, month)
);

CREATE TABLE IF NOT EXISTS crawl_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_threads_status ON threads(status);
CREATE INDEX IF NOT EXISTS idx_threads_forum ON threads(forum_id);
CREATE INDEX IF NOT EXISTS idx_thread_pages_status ON thread_pages(status);
CREATE INDEX IF NOT EXISTS idx_media_status ON media(status);
CREATE INDEX IF NOT EXISTS idx_media_type ON media(type);
"""


class Database:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.executescript(SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        assert self._db is not None, "Database not connected"
        return self._db

    # -- Crawl state --

    async def get_state(self, key: str, default: str | None = None) -> str | None:
        row = await self.db.execute_fetchall(
            "SELECT value FROM crawl_state WHERE key = ?", (key,)
        )
        return row[0][0] if row else default

    async def set_state(self, key: str, value: str) -> None:
        await self.db.execute(
            "INSERT OR REPLACE INTO crawl_state (key, value) VALUES (?, ?)",
            (key, value),
        )
        await self.db.commit()

    # -- Threads --

    async def get_thread(self, thread_id: int) -> dict | None:
        rows = await self.db.execute_fetchall(
            "SELECT * FROM threads WHERE thread_id = ?", (thread_id,)
        )
        return dict(rows[0]) if rows else None

    async def upsert_thread(
        self,
        thread_id: int,
        *,
        forum_id: int | None = None,
        title: str | None = None,
        author: str | None = None,
        reply_count: int | None = None,
        view_count: int | None = None,
        page_count: int = 1,
        first_post_date: str | None = None,
        last_post_date: str | None = None,
        status: ThreadStatus = ThreadStatus.PENDING,
        error_message: str | None = None,
    ) -> None:
        await self.db.execute(
            """INSERT INTO threads
               (thread_id, forum_id, title, author, reply_count, view_count,
                page_count, first_post_date, last_post_date, status, error_message)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(thread_id) DO UPDATE SET
                 forum_id = COALESCE(excluded.forum_id, forum_id),
                 title = COALESCE(excluded.title, title),
                 author = COALESCE(excluded.author, author),
                 reply_count = COALESCE(excluded.reply_count, reply_count),
                 view_count = COALESCE(excluded.view_count, view_count),
                 page_count = COALESCE(excluded.page_count, page_count),
                 first_post_date = COALESCE(excluded.first_post_date, first_post_date),
                 last_post_date = COALESCE(excluded.last_post_date, last_post_date),
                 status = excluded.status,
                 error_message = excluded.error_message,
                 last_crawled = CURRENT_TIMESTAMP
            """,
            (
                thread_id, forum_id, title, author, reply_count, view_count,
                page_count, first_post_date, last_post_date, status.value,
                error_message,
            ),
        )
        await self.db.commit()

    async def update_thread_status(
        self, thread_id: int, status: ThreadStatus, error_message: str | None = None
    ) -> None:
        await self.db.execute(
            """UPDATE threads SET status = ?, error_message = ?,
               last_crawled = CURRENT_TIMESTAMP WHERE thread_id = ?""",
            (status.value, error_message, thread_id),
        )
        await self.db.commit()

    async def get_threads_needing_pages(
        self, forum_ids: list[int] | None = None, limit: int = 100
    ) -> list[dict]:
        if forum_ids:
            placeholders = ",".join("?" * len(forum_ids))
            rows = await self.db.execute_fetchall(
                f"""SELECT * FROM threads
                    WHERE status = 'complete' AND page_count > pages_downloaded
                    AND forum_id IN ({placeholders})
                    ORDER BY thread_id LIMIT ?""",
                (*forum_ids, limit),
            )
        else:
            rows = await self.db.execute_fetchall(
                """SELECT * FROM threads
                   WHERE status = 'complete' AND page_count > pages_downloaded
                   ORDER BY thread_id LIMIT ?""",
                (limit,),
            )
        return [dict(r) for r in rows]

    async def increment_pages_downloaded(self, thread_id: int) -> None:
        await self.db.execute(
            "UPDATE threads SET pages_downloaded = pages_downloaded + 1 WHERE thread_id = ?",
            (thread_id,),
        )
        await self.db.commit()

    # -- Thread pages --

    async def get_page(self, thread_id: int, page_num: int) -> dict | None:
        rows = await self.db.execute_fetchall(
            "SELECT * FROM thread_pages WHERE thread_id = ? AND page_num = ?",
            (thread_id, page_num),
        )
        return dict(rows[0]) if rows else None

    async def upsert_page(
        self,
        thread_id: int,
        page_num: int,
        *,
        status: PageStatus = PageStatus.PENDING,
        html_path: str | None = None,
        post_count: int | None = None,
        file_size: int | None = None,
    ) -> None:
        await self.db.execute(
            """INSERT INTO thread_pages
               (thread_id, page_num, status, html_path, post_count, file_size, downloaded_at)
               VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(thread_id, page_num) DO UPDATE SET
                 status = excluded.status,
                 html_path = COALESCE(excluded.html_path, html_path),
                 post_count = COALESCE(excluded.post_count, post_count),
                 file_size = COALESCE(excluded.file_size, file_size),
                 downloaded_at = CURRENT_TIMESTAMP
            """,
            (thread_id, page_num, status.value, html_path, post_count, file_size),
        )
        await self.db.commit()

    # -- Media --

    async def add_media(
        self,
        url: str,
        media_type: MediaType,
        source_thread_id: int | None = None,
    ) -> None:
        await self.db.execute(
            """INSERT OR IGNORE INTO media (url, type, source_thread_id)
               VALUES (?, ?, ?)""",
            (url, media_type.value, source_thread_id),
        )
        await self.db.commit()

    async def update_media_status(
        self,
        url: str,
        status: MediaStatus,
        *,
        local_path: str | None = None,
        file_size: int | None = None,
        content_type: str | None = None,
        error_message: str | None = None,
    ) -> None:
        await self.db.execute(
            """UPDATE media SET status = ?, local_path = ?, file_size = ?,
               content_type = ?, error_message = ?, downloaded_at = CURRENT_TIMESTAMP
               WHERE url = ?""",
            (status.value, local_path, file_size, content_type, error_message, url),
        )
        await self.db.commit()

    async def get_pending_media(
        self, media_type: MediaType | None = None, limit: int = 100
    ) -> list[dict]:
        if media_type:
            rows = await self.db.execute_fetchall(
                "SELECT * FROM media WHERE status = 'pending' AND type = ? LIMIT ?",
                (media_type.value, limit),
            )
        else:
            rows = await self.db.execute_fetchall(
                "SELECT * FROM media WHERE status = 'pending' LIMIT ?",
                (limit,),
            )
        return [dict(r) for r in rows]

    # -- Events --

    async def upsert_event(
        self,
        year: int,
        month: int,
        *,
        status: str = "pending",
        html_path: str | None = None,
    ) -> None:
        await self.db.execute(
            """INSERT INTO events (year, month, status, html_path, downloaded_at)
               VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(year, month) DO UPDATE SET
                 status = excluded.status,
                 html_path = COALESCE(excluded.html_path, html_path),
                 downloaded_at = CURRENT_TIMESTAMP
            """,
            (year, month, status, html_path),
        )
        await self.db.commit()

    # -- Statistics --

    async def get_stats(self) -> dict:
        stats = {}
        for status in ThreadStatus:
            rows = await self.db.execute_fetchall(
                "SELECT COUNT(*) FROM threads WHERE status = ?", (status.value,)
            )
            stats[f"threads_{status.value}"] = rows[0][0]

        rows = await self.db.execute_fetchall("SELECT COUNT(*) FROM threads")
        stats["threads_total"] = rows[0][0]

        rows = await self.db.execute_fetchall(
            "SELECT COUNT(*) FROM thread_pages WHERE status = 'downloaded'"
        )
        stats["pages_downloaded"] = rows[0][0]

        rows = await self.db.execute_fetchall(
            "SELECT COUNT(*) FROM thread_pages WHERE status = 'pending'"
        )
        stats["pages_pending"] = rows[0][0]

        rows = await self.db.execute_fetchall(
            "SELECT COUNT(*) FROM media WHERE status = 'downloaded'"
        )
        stats["media_downloaded"] = rows[0][0]

        rows = await self.db.execute_fetchall(
            "SELECT COUNT(*) FROM media WHERE status = 'pending'"
        )
        stats["media_pending"] = rows[0][0]

        # Per-forum stats
        rows = await self.db.execute_fetchall(
            """SELECT forum_id, COUNT(*) as cnt,
               SUM(CASE WHEN status = 'complete' THEN 1 ELSE 0 END) as done
               FROM threads WHERE forum_id IS NOT NULL
               GROUP BY forum_id ORDER BY cnt DESC"""
        )
        stats["forums"] = [dict(r) for r in rows]

        return stats

    # -- Recovery --

    async def recover_from_crash(self) -> int:
        cursor = await self.db.execute(
            "UPDATE threads SET status = 'pending' WHERE status = 'crawling'"
        )
        count = cursor.rowcount
        await self.db.commit()
        if count:
            logger.info(f"Recovered {count} in-flight threads after crash")
        return count
