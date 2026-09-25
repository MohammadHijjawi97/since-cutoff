"""A small content-addressed disk cache.

Everything that costs time or money is cached: PyPI metadata, extracted wheels, API diffs,
generated tasks and model answers. Re-running since-cutoff on the same project is therefore
free and reproducible. The cache location can be overridden with ``SINCE_CUTOFF_CACHE``.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


def default_cache_dir() -> Path:
    override = os.environ.get("SINCE_CUTOFF_CACHE")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "since-cutoff" / "Cache"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "since-cutoff"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "since-cutoff"


def stable_hash(*parts: Any) -> str:
    """Hash arbitrary JSON-serialisable parts into a short, stable hex key."""
    payload = json.dumps(parts, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


class DiskCache:
    """JSON documents stored under ``<root>/<namespace>/<key>.json``."""

    def __init__(self, root: Path | None = None, *, enabled: bool = True) -> None:
        self.root = (root or default_cache_dir()).resolve()
        self.enabled = enabled

    def path(self, namespace: str, key: str, suffix: str = ".json") -> Path:
        return self.root / namespace / f"{key}{suffix}"

    def dir(self, namespace: str) -> Path:
        d = self.root / namespace
        d.mkdir(parents=True, exist_ok=True)
        return d

    def get(self, namespace: str, key: str, *, max_age: float | None = None) -> Any | None:
        if not self.enabled:
            return None
        p = self.path(namespace, key)
        try:
            if max_age is not None and time.time() - p.stat().st_mtime > max_age:
                return None
            with p.open(encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def set(self, namespace: str, key: str, value: Any) -> None:
        if not self.enabled:
            return
        p = self.path(namespace, key)
        p.parent.mkdir(parents=True, exist_ok=True)
        # Write atomically so parallel workers never observe half-written files.
        fd, tmp = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(value, fh, ensure_ascii=False, indent=1, default=str)
            Path(tmp).replace(p)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
