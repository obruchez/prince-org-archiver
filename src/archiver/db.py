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
    retry_count INTEGER DEFAULT 0,
    source      TEXT DEFAULT 'live',   -- 'live' | 'wayback'
    snapshot_ts TEXT,                  -- Wayback timestamp when source='wayback'
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

CREATE TABLE IF NOT EXISTS wayback (
    target_type TEXT NOT NULL,           -- 'thread' | 'index'
    target_key  TEXT NOT NULL,           -- thread_id | forum_id (as text)
    forum_id    INTEGER,
    status      TEXT DEFAULT 'pending',  -- pending|recovered|no_capture|error
    snapshot_ts TEXT,
    html_path   TEXT,
    snapshots_found INTEGER DEFAULT 0,
    error_message TEXT,
    attempted_at TIMESTAMP,
    PRIMARY KEY (target_type, target_key)
);

CREATE INDEX IF NOT EXISTS idx_threads_status ON threads(status);
CREATE INDEX IF NOT EXISTS idx_threads_forum ON threads(forum_id);
CREATE INDEX IF NOT EXISTS idx_thread_pages_status ON thread_pages(status);
CREATE INDEX IF NOT EXISTS idx_media_status ON media(status);
CREATE INDEX IF NOT EXISTS idx_media_type ON media(type);
CREATE INDEX IF NOT EXISTS idx_wayback_status ON wayback(status);
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
        await self._migrate()
        await self._db.commit()

    async def _migrate(self) -> None:
        """Apply additive schema migrations for pre-existing databases."""
        cur = await self._db.execute("PRAGMA table_info(thread_pages)")
        cols = {row[1] for row in await cur.fetchall()}
        if "retry_count" not in cols:
            await self._db.execute(
                "ALTER TABLE thread_pages ADD COLUMN retry_count INTEGER DEFAULT 0"
            )
            logger.info("Migrated thread_pages: added retry_count column")
        if "source" not in cols:
            await self._db.execute(
                "ALTER TABLE thread_pages ADD COLUMN source TEXT DEFAULT 'live'"
            )
            logger.info("Migrated thread_pages: added source column")
        if "snapshot_ts" not in cols:
            await self._db.execute(
                "ALTER TABLE thread_pages ADD COLUMN snapshot_ts TEXT"
            )
            logger.info("Migrated thread_pages: added snapshot_ts column")

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
        # A thread needs work while it has any page that is neither
        # downloaded nor terminally failed. Driving this off the actual
        # thread_pages rows (rather than the pages_downloaded counter)
        # guarantees the Phase 2 loop terminates: every page eventually
        # reaches a terminal state ('downloaded' or 'failed').
        unresolved = (
            "EXISTS (SELECT 1 FROM thread_pages p "
            "WHERE p.thread_id = t.thread_id "
            "AND p.status NOT IN ('downloaded', 'failed'))"
        )
        if forum_ids:
            placeholders = ",".join("?" * len(forum_ids))
            rows = await self.db.execute_fetchall(
                f"""SELECT t.* FROM threads t
                    WHERE t.status = 'complete'
                    AND t.forum_id IN ({placeholders})
                    AND {unresolved}
                    ORDER BY t.thread_id LIMIT ?""",
                (*forum_ids, limit),
            )
        else:
            rows = await self.db.execute_fetchall(
                f"""SELECT t.* FROM threads t
                    WHERE t.status = 'complete'
                    AND {unresolved}
                    ORDER BY t.thread_id LIMIT ?""",
                (limit,),
            )
        return [dict(r) for r in rows]

    async def get_pending_pages(self, thread_id: int) -> list[int]:
        """Page numbers for a thread that still need fetching (not terminal)."""
        rows = await self.db.execute_fetchall(
            """SELECT page_num FROM thread_pages
               WHERE thread_id = ? AND status NOT IN ('downloaded', 'failed')
               ORDER BY page_num""",
            (thread_id,),
        )
        return [r[0] for r in rows]

    async def record_page_failure(
        self, thread_id: int, page_num: int, max_retries: int
    ) -> str:
        """Bump a page's retry count; mark it terminally 'failed' once the
        retry budget is exhausted. Returns the resulting page status."""
        rows = await self.db.execute_fetchall(
            "SELECT retry_count FROM thread_pages WHERE thread_id = ? AND page_num = ?",
            (thread_id, page_num),
        )
        retries = ((rows[0][0] or 0) if rows else 0) + 1
        status = (
            PageStatus.FAILED.value
            if retries >= max_retries
            else PageStatus.ERROR.value
        )
        await self.db.execute(
            """INSERT INTO thread_pages
               (thread_id, page_num, status, retry_count, downloaded_at)
               VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(thread_id, page_num) DO UPDATE SET
                 status = excluded.status,
                 retry_count = excluded.retry_count,
                 downloaded_at = CURRENT_TIMESTAMP
            """,
            (thread_id, page_num, status, retries),
        )
        await self.db.commit()
        return status

    async def get_closed_threads_missing_forum(
        self, limit: int = 100, max_attempts: int = 3
    ) -> list[dict]:
        # Exclude threads whose forum_id we have tried and failed to obtain
        # `max_attempts` times: a closed thread in a dead forum may never
        # yield a forum_id via redirect, and without this cap the backfill
        # loop would re-fetch it forever (same trap as the Phase 2 bug).
        rows = await self.db.execute_fetchall(
            """SELECT * FROM threads
               WHERE status = 'closed' AND forum_id IS NULL
               AND retry_count < ?
               ORDER BY thread_id LIMIT ?""",
            (max_attempts, limit),
        )
        return [dict(r) for r in rows]

    async def increment_thread_retry(self, thread_id: int) -> None:
        await self.db.execute(
            "UPDATE threads SET retry_count = retry_count + 1 WHERE thread_id = ?",
            (thread_id,),
        )
        await self.db.commit()

    async def update_thread_forum(self, thread_id: int, forum_id: int) -> None:
        await self.db.execute(
            "UPDATE threads SET forum_id = ?, last_crawled = CURRENT_TIMESTAMP WHERE thread_id = ?",
            (forum_id, thread_id),
        )
        await self.db.commit()

    async def update_thread_metadata(
        self,
        thread_id: int,
        *,
        forum_id: int | None = None,
        first_post_date: str | None = None,
        last_post_date: str | None = None,
    ) -> None:
        """Fill metadata fields, leaving any already-set / unprovided value
        untouched (COALESCE keeps existing data; NULL args are no-ops)."""
        await self.db.execute(
            """UPDATE threads SET
                 forum_id = COALESCE(?, forum_id),
                 first_post_date = COALESCE(?, first_post_date),
                 last_post_date = COALESCE(?, last_post_date),
                 last_crawled = CURRENT_TIMESTAMP
               WHERE thread_id = ?""",
            (forum_id, first_post_date, last_post_date, thread_id),
        )
        await self.db.commit()

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

    # -- Wayback integration --

    async def enrich_thread_from_wayback(
        self,
        thread_id: int,
        *,
        forum_id: int | None = None,
        title: str | None = None,
        author: str | None = None,
        page_count: int | None = None,
    ) -> None:
        """Fill in metadata parsed from a recovered Wayback snapshot
        without touching status/error_message.

        For closed threads the live crawl never parsed the page (it just
        recorded status='closed'); for gated threads the saved stub had a
        garbage title='error'. The Wayback HTML is the only authoritative
        source for these fields, so we overwrite title/author/page_count
        unconditionally when we have a value -- but we keep the existing
        forum_id when one is already set (live crawl + backfill are more
        trustworthy than parsing forum_id out of historical chrome)."""
        await self.db.execute(
            """UPDATE threads SET
                 forum_id = COALESCE(forum_id, ?),
                 title = COALESCE(?, title),
                 author = COALESCE(?, author),
                 page_count = COALESCE(?, page_count),
                 last_crawled = CURRENT_TIMESTAMP
               WHERE thread_id = ?""",
            (forum_id, title, author, page_count, thread_id),
        )
        await self.db.commit()

    async def upsert_wayback_page(
        self,
        thread_id: int,
        page_num: int,
        *,
        html_path: str,
        snapshot_ts: str,
        post_count: int | None = None,
        file_size: int | None = None,
    ) -> None:
        """Record a Wayback snapshot as a thread_pages row with source
        provenance. Replaces any existing live row at (thread_id, page_num)
        -- for gated threads that's the useless 'error' stub the live
        crawl saved; the stub file on disk is left alone but unreferenced."""
        await self.db.execute(
            """INSERT INTO thread_pages
               (thread_id, page_num, status, html_path, post_count,
                file_size, source, snapshot_ts, downloaded_at)
               VALUES (?, ?, 'downloaded', ?, ?, ?, 'wayback', ?,
                       CURRENT_TIMESTAMP)
               ON CONFLICT(thread_id, page_num) DO UPDATE SET
                 status = 'downloaded',
                 html_path = excluded.html_path,
                 post_count = excluded.post_count,
                 file_size = excluded.file_size,
                 source = 'wayback',
                 snapshot_ts = excluded.snapshot_ts,
                 downloaded_at = CURRENT_TIMESTAMP
            """,
            (thread_id, page_num, html_path, post_count, file_size, snapshot_ts),
        )
        await self.db.commit()

    async def get_recovered_wayback_threads(
        self, limit: int | None = None
    ) -> list[dict]:
        """Recovered Wayback thread rows that have an html_path on disk.
        Indexes/no_captures are excluded."""
        sql = (
            "SELECT target_key AS thread_id, forum_id, snapshot_ts, html_path "
            "FROM wayback "
            "WHERE target_type = 'thread' AND status = 'recovered' "
            "AND html_path IS NOT NULL "
            "ORDER BY CAST(target_key AS INTEGER)"
        )
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        rows = await self.db.execute_fetchall(sql)
        return [
            {
                "thread_id": int(r["thread_id"]),
                "forum_id": r["forum_id"],
                "snapshot_ts": r["snapshot_ts"],
                "html_path": r["html_path"],
            }
            for r in rows
        ]

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

    async def requeue_transient_media_errors(self) -> int:
        """Re-pend media rows that errored on transient network failures
        so a plain restart of `crawl media` retries them automatically.

        Distinguishes by error_message prefix:
          * 'Retry exhausted ...'  -> from RetryExhausted in client.py,
            i.e. the network was unreachable through the full 6-attempt
            backoff ladder (~4 min). Re-pendable: the file is likely
            fine, we just couldn't reach the server.
          * 'HTTP <code>'         -> from a non-retryable HTTP status
            (404, 410, 403). Kept terminal -- retrying won't change
            anything and just wastes traffic against prince.org.

        Returns the number of rows re-pended."""
        cur = await self.db.execute(
            "UPDATE media SET status = 'pending', error_message = NULL "
            "WHERE status = 'error' AND error_message LIKE 'Retry exhausted%'"
        )
        await self.db.commit()
        return cur.rowcount

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

    # -- Wayback recovery worklist --

    async def seed_wayback_threads(self, kind: str) -> int:
        """Populate the wayback worklist with thread targets.

        kind='gated'  -> complete-but-error-stub threads (forum_id IS NULL)
        kind='closed' -> threads in permanently closed forums
        Existing rows are left untouched (idempotent / resumable)."""
        if kind == "gated":
            where = "status = 'complete' AND forum_id IS NULL"
        elif kind == "closed":
            where = "status = 'closed'"
        else:
            raise ValueError(f"unknown kind {kind!r}")
        cur = await self.db.execute(
            f"""INSERT OR IGNORE INTO wayback
                (target_type, target_key, forum_id, status)
                SELECT 'thread', CAST(thread_id AS TEXT), forum_id, 'pending'
                FROM threads WHERE {where}""",
        )
        await self.db.commit()
        return cur.rowcount

    async def seed_wayback_index(self, forum_ids: list[int]) -> int:
        n = 0
        for fid in forum_ids:
            cur = await self.db.execute(
                """INSERT OR IGNORE INTO wayback
                   (target_type, target_key, forum_id, status)
                   VALUES ('index', ?, ?, 'pending')""",
                (str(fid), fid),
            )
            n += cur.rowcount
        await self.db.commit()
        return n

    async def sync_wayback_forum_ids(self) -> int:
        """Reconcile the wayback worklist with forum_ids resolved later
        (e.g. by `crawl backfill`).

        The worklist is seeded once with INSERT OR IGNORE, so rows seeded
        before backfill keep their original (NULL) forum_id even after
        `threads.forum_id` is filled. Without this, those targets would be
        re-classified `no_forum` and the backfill wasted. We:
          1. copy the now-known forum_id onto stale-NULL thread rows, and
          2. re-pend any target deferred as `no_forum` that now has one.
        Returns the number of deferred targets re-pended."""
        await self.db.execute(
            """UPDATE wayback
                 SET forum_id = (
                     SELECT t.forum_id FROM threads t
                     WHERE t.thread_id = CAST(wayback.target_key AS INTEGER))
               WHERE target_type = 'thread'
                 AND forum_id IS NULL
                 AND EXISTS (
                     SELECT 1 FROM threads t
                     WHERE t.thread_id = CAST(wayback.target_key AS INTEGER)
                       AND t.forum_id IS NOT NULL)""",
        )
        cur = await self.db.execute(
            """UPDATE wayback SET status = 'pending', error_message = NULL
               WHERE target_type = 'thread' AND status = 'no_forum'
                 AND forum_id IS NOT NULL""",
        )
        await self.db.commit()
        return cur.rowcount

    async def get_pending_wayback(
        self, target_type: str | None = None, limit: int = 100
    ) -> list[dict]:
        if target_type:
            rows = await self.db.execute_fetchall(
                """SELECT * FROM wayback WHERE status = 'pending'
                   AND target_type = ? ORDER BY target_key LIMIT ?""",
                (target_type, limit),
            )
        else:
            rows = await self.db.execute_fetchall(
                """SELECT * FROM wayback WHERE status = 'pending'
                   ORDER BY target_type, target_key LIMIT ?""",
                (limit,),
            )
        return [dict(r) for r in rows]

    async def update_wayback(
        self,
        target_type: str,
        target_key: str,
        *,
        status: str,
        snapshot_ts: str | None = None,
        html_path: str | None = None,
        snapshots_found: int | None = None,
        error_message: str | None = None,
    ) -> None:
        await self.db.execute(
            """UPDATE wayback SET
                 status = ?, snapshot_ts = ?, html_path = ?,
                 snapshots_found = COALESCE(?, snapshots_found),
                 error_message = ?, attempted_at = CURRENT_TIMESTAMP
               WHERE target_type = ? AND target_key = ?""",
            (status, snapshot_ts, html_path, snapshots_found,
             error_message, target_type, target_key),
        )
        await self.db.commit()

    async def wayback_stats(self) -> dict:
        rows = await self.db.execute_fetchall(
            "SELECT target_type, status, COUNT(*) AS n FROM wayback "
            "GROUP BY target_type, status"
        )
        return {f"{r[0]}_{r[1]}": r[2] for r in rows}

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
