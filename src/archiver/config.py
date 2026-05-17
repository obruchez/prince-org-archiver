from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    data_dir: Path = Path("data")
    rate: float = 0.5  # requests per second
    concurrency: int = 3
    burst: int = 5
    max_requests: int | None = None
    max_duration_seconds: int | None = None
    start_id: int = 1
    end_id: int = 475_000
    forum_filter: int | None = None
    priority_forums: list[int] = field(default_factory=lambda: [7, 100, 8])
    retry_errors: bool = False
    user_agent: str = "PrinceOrgArchiver/0.1 (archival project; contact: prince-org-archiver@example.com)"
    request_timeout: float = 30.0
    error_budget: int = 50  # pause after this many consecutive errors
    adaptive_threshold: float = 5.0  # double delay if response > this many seconds
    base_url: str = "https://prince.org"
    wayback_rate: float = 1.0  # requests/sec against web.archive.org (be gentle)

    @property
    def html_dir(self) -> Path:
        return self.data_dir / "html"

    @property
    def threads_dir(self) -> Path:
        return self.html_dir / "threads"

    @property
    def events_dir(self) -> Path:
        return self.html_dir / "events"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def avatars_dir(self) -> Path:
        return self.media_dir / "avatars"

    @property
    def emoticons_dir(self) -> Path:
        return self.media_dir / "emoticons"

    @property
    def post_images_dir(self) -> Path:
        return self.media_dir / "post_images"

    @property
    def wayback_dir(self) -> Path:
        return self.data_dir / "wayback"

    @property
    def wayback_threads_dir(self) -> Path:
        return self.wayback_dir / "threads"

    @property
    def wayback_index_dir(self) -> Path:
        return self.wayback_dir / "index"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "archive.db"

    @property
    def log_path(self) -> Path:
        return self.data_dir / "logs" / "archiver.log"

    def ensure_dirs(self) -> None:
        for d in [
            self.threads_dir,
            self.events_dir,
            self.avatars_dir,
            self.emoticons_dir,
            self.post_images_dir,
            self.wayback_threads_dir,
            self.wayback_index_dir,
            self.data_dir / "logs",
        ]:
            d.mkdir(parents=True, exist_ok=True)
