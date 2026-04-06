import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path

from archiver.config import Config
from archiver.models import ThreadMetadata


def _atomic_write(path: Path, data: bytes) -> None:
    """Write data to a file atomically using temp file + rename."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        os.write(fd, data)
        os.fsync(fd)
        os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def thread_dir(config: Config, thread_id: int) -> Path:
    bucket = f"{thread_id // 1000:03d}"
    return config.threads_dir / bucket / str(thread_id)


def save_thread_page(
    config: Config, thread_id: int, page_num: int, html: bytes
) -> Path:
    d = thread_dir(config, thread_id)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"page_{page_num}.html"
    _atomic_write(path, html)
    return path


def save_thread_metadata(config: Config, metadata: ThreadMetadata) -> Path:
    d = thread_dir(config, metadata.thread_id)
    d.mkdir(parents=True, exist_ok=True)
    path = d / "metadata.json"

    data = json.dumps(asdict(metadata), indent=2, ensure_ascii=False).encode()
    _atomic_write(path, data)
    return path


def save_events_page(config: Config, year: int, month: int, html: bytes) -> Path:
    d = config.events_dir / str(year)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{month:02d}.html"
    _atomic_write(path, html)
    return path
