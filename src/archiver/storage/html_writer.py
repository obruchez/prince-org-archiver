import json
from dataclasses import asdict
from pathlib import Path

from archiver.config import Config
from archiver.models import ThreadMetadata


def thread_dir(config: Config, thread_id: int) -> Path:
    bucket = f"{thread_id // 1000:03d}"
    return config.threads_dir / bucket / str(thread_id)


def save_thread_page(
    config: Config, thread_id: int, page_num: int, html: bytes
) -> Path:
    d = thread_dir(config, thread_id)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"page_{page_num}.html"
    path.write_bytes(html)
    return path


def save_thread_metadata(config: Config, metadata: ThreadMetadata) -> Path:
    d = thread_dir(config, metadata.thread_id)
    d.mkdir(parents=True, exist_ok=True)
    path = d / "metadata.json"

    data = asdict(metadata)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return path


def save_events_page(config: Config, year: int, month: int, html: bytes) -> Path:
    d = config.events_dir / str(year)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{month:02d}.html"
    path.write_bytes(html)
    return path
