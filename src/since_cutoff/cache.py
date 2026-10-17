"""A small content-addressed disk cache.

Everything that costs time or money is cached: PyPI metadata, extracted wheels, API diffs,
generated tasks and model answers. Re-running since-cutoff on the same project is therefore
free and reproducible. The cache location can be overridden with ``SINCE_CUTOFF_CACHE``.

The extracted sources are the bulk of it (a few MB per package version, gigabytes over time),
so they have a cap: ``SINCE_CUTOFF_CACHE_MAX_MB`` (:func:`max_sources_bytes`), kept by
:func:`sources_over_cap` and :func:`evict_tree`, which :class:`since_cutoff.pypi.PyPI` runs
after it extracts a new version. ``since-cutoff cache info`` and ``cache clear`` use
:func:`stats` and :func:`clear`.

This module is imported by every command, ``--help`` included, so it imports only what the
cache's bookkeeping needs; ``tempfile`` is loaded when something is written.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import shutil
import sys
import time
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# What ``cache info`` lists and ``cache clear`` removes, in this order.
NAMESPACES = (
    "pypi",
    "sources",
    "sources-meta",
    "diffs",
    "tasks",
    "answers",
    "notes",
    "envs",
    "models",
)
# What ``since-cutoff run`` got from the model: making it again costs money.
MODEL_OUTPUT = ("tasks", "answers", "notes")
# ``cache clear --sources``, ``--diffs`` and ``--pypi``: the namespaces each flag removes.
CLEAR_FLAGS = {
    "sources": ("sources", "sources-meta"),
    "diffs": ("diffs",),
    "pypi": ("pypi",),
}
MB = 1024 * 1024
# The file at the root of an extracted source tree (``sources/<name>-<version>/``): its import
# names and requirements, written last, so a tree without it is not published yet or broken.
# Its mtime is when the tree was last used, and ``bytes`` is the size of the tree.
SOURCES_MARKER = ".since-cutoff.json"
DEFAULT_MAX_SOURCES_MB = 2048
# A source tree being deleted is first renamed to ``<key>.tmp-evicted-<random>``. One left behind
# (the process stopped mid-delete) is no tree: the next trim deletes it.
EVICTED = ".tmp-evicted"
# A tree used this recently is never evicted: another since-cutoff process (the MCP server, a
# scan in another terminal) may still be reading it.
SOURCES_GRACE = 3600.0


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


def max_sources_bytes(env: Any = None) -> int | None:
    """The cap on the extracted sources, in bytes, from ``SINCE_CUTOFF_CACHE_MAX_MB``
    (default :data:`DEFAULT_MAX_SOURCES_MB`); None when it is 0 or less: no cap. A value that
    is not a number (``inf`` and ``nan`` included, which ``float`` accepts) is reported and the
    default used."""
    raw = (os.environ if env is None else env).get("SINCE_CUTOFF_CACHE_MAX_MB", "")
    if not raw.strip():
        return DEFAULT_MAX_SOURCES_MB * MB
    try:
        megabytes = float(raw)
        if not math.isfinite(megabytes):
            raise ValueError(raw)
    except ValueError:
        import logging

        logging.getLogger(__name__).warning(
            "SINCE_CUTOFF_CACHE_MAX_MB=%r is not a number; the sources cap is %d MB",
            raw,
            DEFAULT_MAX_SOURCES_MB,
        )
        return DEFAULT_MAX_SOURCES_MB * MB
    if megabytes <= 0:  # 0 switches the cap off
        return None
    return int(megabytes * MB)


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
        write_json(p, value)


def write_json(path: Path, value: Any) -> None:
    """Write ``value`` to ``path`` atomically, so parallel workers never observe a half-written
    file: a temporary file next to it, renamed over it."""
    import tempfile

    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(value, fh, ensure_ascii=False, indent=1, default=str)
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# ------------------------------------------------------------------- sizes
def tree_size(path: Path) -> tuple[int, int]:
    """``(files, bytes)`` of a file or of everything below a directory. Symbolic links count
    as themselves, never what they point to; what cannot be read is left out."""
    files = size = 0
    try:
        top = path.lstat()
    except OSError:
        return 0, 0
    if not path.is_dir() or path.is_symlink():
        return 1, top.st_size
    pending = [str(path)]
    while pending:
        try:
            with os.scandir(pending.pop()) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(entry.path)
                        else:
                            files += 1
                            size += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return files, size


@dataclass(frozen=True)
class NamespaceStats:
    """One namespace of the cache, for ``cache info``: how many entries (its files or
    folders), how many files in all, their size, and ``oldest``, the earliest
    :func:`last_used` of its entries (None when it is empty). Only the source trees record
    their use; for every other kind it is when its oldest entry was written. What an eviction
    left half deleted (:data:`EVICTED`) counts in the size but is no entry."""

    name: str
    entries: int
    files: int
    bytes: int
    oldest: float | None

    @property
    def model_output(self) -> bool:
        return self.name in MODEL_OUTPUT


def stats(root: Path, namespaces: Iterable[str] = NAMESPACES) -> list[NamespaceStats]:
    """The size of each namespace of the cache at ``root``, which may not exist."""
    out = []
    for name in namespaces:
        folder = root / name
        entries = files = size = 0
        oldest: float | None = None
        try:
            children = list(os.scandir(folder))
        except OSError:
            children = []
        for child in children:
            n, b = tree_size(Path(child.path))
            files += n
            size += b
            if EVICTED in child.name:
                continue
            entries += 1
            used = last_used(Path(child.path))
            if used is not None and (oldest is None or used < oldest):
                oldest = used
        out.append(NamespaceStats(name, entries, files, size, oldest))
    return out


def last_used(entry: Path) -> float | None:
    """When a cache entry was last used: a source tree's marker mtime (touched at each use),
    else the entry's own mtime, which is when it was written (reading a JSON entry does not
    touch it)."""
    for candidate in (entry / SOURCES_MARKER, entry):
        try:
            return candidate.stat().st_mtime
        except OSError:
            continue
    return None


def clear(root: Path, namespaces: Iterable[str] = NAMESPACES) -> list[str]:
    """Remove these namespaces from the cache at ``root`` and return the ones that were there.
    The root itself goes too when nothing else is left in it (it was ours alone)."""
    removed = []
    for name in namespaces:
        folder = root / name
        if folder.is_dir():
            shutil.rmtree(folder, ignore_errors=True)
            removed.append(name)
    with contextlib.suppress(OSError):
        root.rmdir()
    return removed


# ----------------------------------------------------------------- sources
@dataclass(frozen=True)
class SourceEntry:
    """One published source tree: ``sources/<key>/``, its size and when it was last used."""

    key: str
    path: Path
    bytes: int
    last_used: float


def source_entries(root: Path) -> list[SourceEntry]:
    """The published source trees under ``root/sources``, least recently used first. A tree
    extracted before the size was recorded in its marker is measured once (its files, the
    marker aside, as PyPI records it), and the size is written into the marker, whose mtime is
    kept: measuring is not using."""
    sources = root / "sources"
    entries: list[SourceEntry] = []
    try:
        children = [c for c in os.scandir(sources) if c.is_dir(follow_symlinks=False)]
    except OSError:
        return entries
    for child in children:
        if EVICTED in child.name:
            continue  # being deleted
        marker = Path(child.path) / SOURCES_MARKER
        try:
            stat = marker.stat()
            data = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue  # being extracted, or broken: PyPI replaces it, not the cap
        if not isinstance(data, dict):
            continue
        size = data.get("bytes")
        if not isinstance(size, int) or isinstance(size, bool):
            size = tree_size(Path(child.path))[1] - stat.st_size  # the files, the marker aside
            try:
                write_json(marker, {**data, "bytes": size})
                os.utime(marker, (stat.st_atime, stat.st_mtime))
            except OSError:
                pass
        entries.append(SourceEntry(child.name, Path(child.path), size, stat.st_mtime))
    entries.sort(key=lambda e: (e.last_used, e.key))
    return entries


def sources_over_cap(
    root: Path,
    max_bytes: int,
    keep: Collection[str] = (),
    *,
    now: float | None = None,
    grace: float = SOURCES_GRACE,
) -> list[SourceEntry]:
    """The source trees to evict so that those under ``root/sources`` fit in ``max_bytes``:
    the least recently used first, never one of ``keep`` (the trees the running scan uses) and
    never one used in the last ``grace`` seconds (another process may be reading it)."""
    entries = source_entries(root)
    total = sum(e.bytes for e in entries)
    now = time.time() if now is None else now
    evict: list[SourceEntry] = []
    for entry in entries:
        if total <= max_bytes:
            break
        if entry.key in keep or now - entry.last_used < grace:
            continue
        evict.append(entry)
        total -= entry.bytes
    return evict


def evict_tree(path: Path) -> bool:
    """Remove a source tree: renamed away first, to a name of its own (one an earlier eviction
    left behind is not in the way), so that it disappears at once rather than file by file,
    then deleted. False when it could not be renamed (on Windows, a tree a file of which is
    open: it is in use, and stays)."""
    gone = path.with_name(f"{path.name}{EVICTED}-{os.urandom(4).hex()}")
    try:
        path.rename(gone)
    except OSError:
        return False
    shutil.rmtree(gone, ignore_errors=True)
    return True


def remove_evicted_leftovers(root: Path) -> None:
    """Delete what an interrupted :func:`evict_tree` left under ``root/sources``: trees
    already renamed away, which nothing reads."""
    try:
        leftovers = [c.path for c in os.scandir(root / "sources") if EVICTED in c.name]
    except OSError:
        return
    for path in leftovers:
        shutil.rmtree(path, ignore_errors=True)


def touch(path: Path) -> None:
    """Set a file's mtime to now (a source tree's marker: the tree was used)."""
    with contextlib.suppress(OSError):
        os.utime(path, None)
