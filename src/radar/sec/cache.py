"""URL-addressed disk storage for SEC metadata and immutable raw filings."""

import hashlib
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


class CacheMissError(RuntimeError):
    """Offline operation requires a document that is not cached."""


class CacheCorruptionError(RuntimeError):
    """A cached metadata response cannot be decoded."""


@dataclass(frozen=True)
class CacheEntry:
    content: bytes
    stored_at: float

    def is_fresh(self, ttl_seconds: int) -> bool:
        return 0 <= time.time() - self.stored_at < ttl_seconds


def atomic_write(path: Path, content: bytes) -> None:
    """Replace one complete cache entry without exposing a partial response."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=".download-", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class DiskCache:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)

    def path_for(self, url: str, permanent: bool = False) -> Path:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        if permanent:
            return self.data_dir / "raw" / f"{digest}.html"
        return self.data_dir / "cache" / "sec" / f"{digest}.json"

    def read(self, url: str, permanent: bool = False) -> CacheEntry | None:
        path = self.path_for(url, permanent)
        try:
            with path.open("rb") as stream:
                content = stream.read()
                stored_at = os.fstat(stream.fileno()).st_mtime
        except FileNotFoundError:
            return None
        return CacheEntry(content, stored_at)

    def write(self, url: str, content: bytes, permanent: bool = False) -> Path:
        path = self.path_for(url, permanent)
        atomic_write(path, content)
        return path
