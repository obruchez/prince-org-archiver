from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class ThreadStatus(str, Enum):
    PENDING = "pending"
    CRAWLING = "crawling"
    COMPLETE = "complete"
    NOT_FOUND = "not_found"
    CLOSED = "closed"
    ERROR = "error"


class PageStatus(str, Enum):
    PENDING = "pending"
    DOWNLOADED = "downloaded"
    ERROR = "error"
    FAILED = "failed"  # terminal: gave up after max retries


class MediaStatus(str, Enum):
    PENDING = "pending"
    DOWNLOADED = "downloaded"
    ERROR = "error"
    SKIPPED = "skipped"


class MediaType(str, Enum):
    AVATAR = "avatar"
    EMOTICON = "emoticon"
    POST_IMAGE = "post_image"
    GALLERY = "gallery"


class ResponseType(str, Enum):
    THREAD_FOUND = "thread_found"
    NOT_FOUND = "not_found"
    FORUM_CLOSED = "forum_closed"
    ERROR = "error"


@dataclass
class ThreadMetadata:
    thread_id: int
    forum_id: int | None = None
    forum_name: str | None = None
    title: str | None = None
    author: str | None = None
    reply_count: int | None = None
    view_count: int | None = None
    page_count: int = 1
    first_post_date: str | None = None
    last_post_date: str | None = None
    media_urls: list[str] = field(default_factory=list)


@dataclass
class ParsedThreadPage:
    thread_id: int
    page_num: int
    response_type: ResponseType
    metadata: ThreadMetadata | None = None
    post_count: int = 0
    media_urls: list[str] = field(default_factory=list)
    raw_html: bytes = b""
