"""A tiny JSON-on-disk cache, so stopping and re-running a search repeats no work."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path


class DiskCache:
    def __init__(self, directory: Path | None):
        self.directory = directory

    def _path(self, key: str) -> Path:
        return self.directory / f"{hashlib.sha1(key.encode()).hexdigest()}.json"

    def get(self, key: str, max_age_days: float):
        if self.directory is None:
            return None
        path = self._path(key)
        try:
            if time.time() - path.stat().st_mtime > max_age_days * 86400:
                return None
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def set(self, key: str, value) -> None:
        if self.directory is None:
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        self._path(key).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
