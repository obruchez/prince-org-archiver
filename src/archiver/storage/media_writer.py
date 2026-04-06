import hashlib
import re
from pathlib import Path
from urllib.parse import urlparse

from archiver.config import Config
from archiver.models import MediaType


def media_path(config: Config, url: str, media_type: MediaType) -> Path:
    parsed = urlparse(url)
    filename = Path(parsed.path).name

    if media_type == MediaType.AVATAR:
        return config.avatars_dir / filename
    elif media_type == MediaType.EMOTICON:
        return config.emoticons_dir / filename
    elif media_type == MediaType.POST_IMAGE:
        # Use hash to avoid filename collisions
        url_hash = hashlib.md5(url.encode()).hexdigest()[:12]
        ext = Path(parsed.path).suffix or ".bin"
        return config.post_images_dir / f"{url_hash}{ext}"
    elif media_type == MediaType.GALLERY:
        url_hash = hashlib.md5(url.encode()).hexdigest()[:12]
        ext = Path(parsed.path).suffix or ".bin"
        return config.media_dir / "gallery" / f"{url_hash}{ext}"
    else:
        return config.media_dir / "other" / filename


def save_media(config: Config, url: str, media_type: MediaType, data: bytes) -> Path:
    from archiver.storage.html_writer import _atomic_write

    path = media_path(config, url, media_type)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, data)
    return path


def classify_media_url(url: str) -> MediaType | None:
    if "avatars/" in url or ":444/" in url:
        return MediaType.AVATAR
    elif "/i/s/" in url or "/i/" in url and url.endswith(".gif"):
        return MediaType.EMOTICON
    elif "gallery" in url:
        return MediaType.GALLERY
    elif url.startswith("https://prince.org") or url.startswith("http://prince.org"):
        return MediaType.POST_IMAGE
    return None
