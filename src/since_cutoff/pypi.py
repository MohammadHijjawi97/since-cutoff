"""PyPI access: release dates, "which version existed on date X", and source extraction.

Sources are taken from wheels (falling back to sdists) and only ``.py``/``.pyi`` files are
extracted, with path and size checks. Nothing is installed and no package code is executed.
"""

from __future__ import annotations

import email.parser
import io
import json
import re
import shutil
import tarfile
import tempfile
import threading
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from since_cutoff import net
from since_cutoff.cache import DiskCache
from since_cutoff.errors import PackageIndexError

PYPI_JSON = "https://pypi.org/pypi/{name}/json"
METADATA_TTL = 12 * 3600
SOURCE_SCHEMA = 2
_SOURCE_SUFFIXES = (".py", ".pyi")
_NON_PACKAGE_DIRS = {"test", "tests", "docs", "doc", "examples", "example", "benchmarks", "scripts"}
_MAX_MEMBER_BYTES = 8 * 1024 * 1024
_MAX_TOTAL_BYTES = 512 * 1024 * 1024
_MAX_MEMBERS = 50_000
_MARKER = ".since-cutoff.json"


@dataclass(frozen=True)
class Release:
    version: str
    uploaded: datetime
    yanked: bool
    files: tuple[dict[str, Any], ...]

    @property
    def parsed(self) -> Version:
        return Version(self.version)

    @property
    def is_final(self) -> bool:
        v = self.parsed
        return not (v.is_prerelease or v.is_devrelease)


@dataclass(frozen=True)
class SourceTree:
    """Extracted sources of one distribution version."""

    name: str
    version: str
    root: Path
    import_names: tuple[str, ...]
    requires: tuple[str, ...] = ()


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class PyPI:
    def __init__(self, cache: DiskCache, *, max_download_mb: float = 80.0) -> None:
        self.cache = cache
        self.max_download_bytes = int(max_download_mb * 1024 * 1024)
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock(self, key: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(key, threading.Lock())

    # ----------------------------------------------------------------- metadata
    def project(self, name: str) -> dict[str, Any]:
        key = canonicalize_name(name)
        cached = self.cache.get("pypi", key, max_age=METADATA_TTL)
        if cached is not None:
            return dict(cached)
        try:
            data = net.get_json(PYPI_JSON.format(name=key))
            slim = {
                "info": {k: data["info"].get(k) for k in ("name", "version", "summary")},
                "releases": dict(data.get("releases") or {}),
            }
        except net.HTTPError as exc:
            if exc.status == 404:
                raise PackageIndexError(f"'{name}' is not on PyPI") from exc
            raise PackageIndexError(f"could not reach PyPI for '{name}': {exc.reason}") from exc
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise PackageIndexError(f"PyPI returned an unexpected response for '{name}'") from exc
        self.cache.set("pypi", key, slim)
        return slim

    def releases(self, name: str) -> list[Release]:
        out: list[Release] = []
        for version, files in self.project(name)["releases"].items():
            if not files:
                continue
            try:
                Version(version)
                uploaded = min(_parse_time(f["upload_time_iso_8601"]) for f in files)
            except (InvalidVersion, KeyError, ValueError, TypeError):
                continue
            yanked = all(f.get("yanked", False) for f in files)
            out.append(Release(version, uploaded, yanked, tuple(files)))
        out.sort(key=lambda r: r.parsed)
        return out

    def release(self, name: str, version: str) -> Release:
        try:
            want = Version(version)
        except InvalidVersion:
            raise PackageIndexError(f"{name}=={version} is not a valid (PEP 440) version") from None
        releases = self.releases(name)
        for r in releases:
            if r.parsed == want:
                return r
        if want.local:  # torch==2.5.1+cpu: the public 2.5.1 is what PyPI has
            public = Version(want.public)
            for r in releases:
                if r.parsed == public:
                    return r
        raise PackageIndexError(f"{name}=={version} is not on PyPI")

    def latest(self, name: str) -> Release:
        finals = [r for r in self.releases(name) if r.is_final and not r.yanked]
        if not finals:
            raise PackageIndexError(f"'{name}' has no final releases on PyPI")
        return finals[-1]

    def version_at(self, name: str, when: date) -> Release | None:
        """The highest final, non-yanked release that was published on or before ``when``."""
        return self.best_match(name, "", when)

    def best_match(self, name: str, specifier: str, when: date | None = None) -> Release | None:
        """Newest final, non-yanked release matching ``specifier`` (and published by ``when``)."""
        try:
            spec = SpecifierSet(specifier)
        except InvalidSpecifier:
            spec = SpecifierSet()
        best: Release | None = None
        for r in self.releases(name):
            if not r.is_final or r.yanked or (when is not None and r.uploaded.date() > when):
                continue
            if r.parsed not in spec:
                continue
            if best is None or r.parsed > best.parsed:
                best = r
        return best

    # ------------------------------------------------------------------ sources
    def source(self, name: str, version: str) -> SourceTree:
        """Download (once) and extract the Python sources of ``name==version``."""
        key = f"{canonicalize_name(name)}-{version}"
        with self._lock(key):
            return self._source(name, version, key)

    def _source(self, name: str, version: str, key: str) -> SourceTree:
        sources = self.cache.root / "sources"
        root = sources / key
        cached = _read_marker(root)
        if cached is not None:
            return SourceTree(name, version, root, cached[0], cached[1])

        rel = self.release(name, version)
        artifact = _pick_artifact(rel.files, allow_yanked=True)
        if artifact is None:
            raise PackageIndexError(f"{name}=={version} has no wheel or sdist on PyPI")
        size = int(artifact.get("size") or 0)
        if size > self.max_download_bytes:
            raise PackageIndexError(
                f"{name}=={version} is {size / 1e6:.0f} MB, above the "
                f"{self.max_download_bytes / 1e6:.0f} MB download limit (--max-download-mb)"
            )
        filename = artifact["filename"]
        try:
            blob = net.request(artifact["url"], timeout=300, max_bytes=self.max_download_bytes + 1)
        except net.HTTPError as exc:
            raise PackageIndexError(f"could not download {filename}: {exc.reason}") from exc

        sources.mkdir(parents=True, exist_ok=True)
        # A unique directory per extraction, published with an atomic rename, so concurrent
        # since-cutoff processes sharing the cache can never observe half-written trees.
        tmp = Path(tempfile.mkdtemp(dir=sources, prefix=f"{key}.tmp-"))
        try:
            try:
                if filename.endswith(".whl"):
                    files = _extract_zip(blob, tmp, strip_first=False)
                    package_root = tmp
                    import_names = _wheel_import_names(blob, files, tmp)
                    requires = _requires_from_zip(blob, r"[^/]+\.dist-info/METADATA")
                else:
                    if filename.endswith(".zip"):
                        _extract_zip(blob, tmp / "_x", strip_first=True)
                        requires = _requires_from_zip(blob, r"[^/]+/PKG-INFO")
                    else:
                        _extract_tar(blob, tmp / "_x")
                        requires = _requires_from_tar(blob)
                    package_root = _sdist_package_root(tmp / "_x")
                    import_names = _refine_namespaces(
                        package_root, _import_names_from_tree(package_root)
                    )
            except (
                zipfile.BadZipFile,
                zipfile.LargeZipFile,
                tarfile.TarError,
                EOFError,
                UnicodeDecodeError,
                OSError,
                ValueError,
            ) as exc:
                raise PackageIndexError(f"could not extract {filename}: {exc}") from exc
            if not import_names:
                raise PackageIndexError(f"could not find importable modules in {filename}")
            (package_root / _MARKER).write_text(
                json.dumps(
                    {"schema": SOURCE_SCHEMA, "import_names": import_names, "requires": requires}
                ),
                encoding="utf-8",
            )
            try:
                package_root.rename(root)
            except OSError:
                if _read_marker(root) is None:  # a stale or broken tree: replace it
                    shutil.rmtree(root, ignore_errors=True)
                    package_root.rename(root)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        cached = _read_marker(root)
        if cached is None:
            raise PackageIndexError(f"could not publish the sources of {name}=={version}")
        return SourceTree(name, version, root, cached[0], cached[1])


# --------------------------------------------------------------------- helpers
def _read_marker(root: Path) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    try:
        data = json.loads((root / _MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema") != SOURCE_SCHEMA:
        return None
    return tuple(data.get("import_names") or ()), tuple(data.get("requires") or ())


def _pick_artifact(
    files: tuple[dict[str, Any], ...], *, allow_yanked: bool = False
) -> dict[str, Any] | None:
    for include_yanked in (False, True) if allow_yanked else (False,):
        pool = [f for f in files if include_yanked or not f.get("yanked")]
        wheels = [f for f in pool if f["filename"].endswith(".whl")]
        if wheels:
            # Pure-Python wheels first; otherwise any wheel works because we only read sources.
            wheels.sort(key=lambda f: ("-none-any" not in f["filename"], int(f.get("size") or 0)))
            return wheels[0]
        sdists = [f for f in pool if f["filename"].endswith((".tar.gz", ".zip"))]
        if sdists:
            return sdists[0]
    return None


def _safe_target(dest: Path, member: str) -> Path | None:
    """Map an archive member to a path inside ``dest``, or None if it would escape it."""
    name = member.replace("\\", "/")
    parts = PurePosixPath(name).parts
    if (
        not parts
        or PurePosixPath(name).is_absolute()
        or PureWindowsPath(name).drive
        or any(p in ("", ".", "..") or ":" in p for p in parts)
    ):
        return None
    target = dest.joinpath(*parts)
    try:
        target.resolve().relative_to(dest.resolve())
    except ValueError:
        return None
    return target


def _wanted(member: str) -> bool:
    return member.endswith(_SOURCE_SUFFIXES) or member.endswith("py.typed")


class _Budget:
    def __init__(self) -> None:
        self.total = 0
        self.count = 0

    def take(self, n: int) -> None:
        self.total += n
        self.count += 1
        if self.total > _MAX_TOTAL_BYTES or self.count > _MAX_MEMBERS:
            raise ValueError("archive expands beyond the extraction limits")


def _extract_zip(blob: bytes, dest: Path, *, strip_first: bool) -> list[str]:
    names: list[str] = []
    budget = _Budget()
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename
            names.append(name)
            if not _wanted(name) or info.file_size > _MAX_MEMBER_BYTES:
                continue
            rel = name.split("/", 1)[1] if strip_first and "/" in name else name
            target = _safe_target(dest, rel)
            if target is None:
                continue
            with zf.open(info) as fh:
                data = fh.read(_MAX_MEMBER_BYTES + 1)
            if len(data) > _MAX_MEMBER_BYTES:
                continue
            budget.take(len(data))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    return names


def _extract_tar(blob: bytes, dest: Path) -> list[str]:
    names: list[str] = []
    budget = _Budget()
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:*") as tf:
        for member in tf:
            if not member.isfile():
                continue
            names.append(member.name)
            if (
                not _wanted(member.name)
                or "/" not in member.name
                or member.size > _MAX_MEMBER_BYTES
            ):
                continue
            target = _safe_target(dest, member.name.split("/", 1)[1])
            fh = tf.extractfile(member)
            if target is None or fh is None:
                continue
            data = fh.read(_MAX_MEMBER_BYTES + 1)
            if len(data) > _MAX_MEMBER_BYTES:
                continue
            budget.take(len(data))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    return names


def _parse_requires(metadata: str) -> list[str]:
    msg = email.parser.Parser().parsestr(metadata, headersonly=True)
    return [r.strip() for r in msg.get_all("Requires-Dist") or [] if r.strip()]


def _requires_from_zip(blob: bytes, pattern: str) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        for n in zf.namelist():
            if re.fullmatch(pattern, n):
                return _parse_requires(zf.read(n)[:1_000_000].decode("utf-8", "replace"))
    return []


def _requires_from_tar(blob: bytes) -> list[str]:
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:*") as tf:
        for member in tf:
            if member.isfile() and re.fullmatch(r"[^/]+/PKG-INFO", member.name):
                fh = tf.extractfile(member)
                if fh is not None:
                    return _parse_requires(fh.read(1_000_000).decode("utf-8", "replace"))
    return []


def _wheel_import_names(blob: bytes, files: list[str], root: Path) -> list[str]:
    names: list[str] = []
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        top = [n for n in zf.namelist() if re.match(r"[^/]+\.dist-info/top_level\.txt$", n)]
        if top:
            listed = [
                line.strip() for line in zf.read(top[0]).decode("utf-8", "replace").splitlines()
            ]
            names = [n.replace("/", ".") for n in listed if _is_public_top(n)]
    if not names:
        tops: set[str] = set()
        for f in files:
            first = f.split("/", 1)[0]
            if first.endswith((".dist-info", ".data")):
                continue
            if "/" in f:
                tops.add(first)
            elif f.endswith(_SOURCE_SUFFIXES):
                tops.add(first.rsplit(".", 1)[0])
        names = [t for t in tops if _is_public_top(t)]
    return _refine_namespaces(root, sorted(set(names)))


def _refine_namespaces(root: Path, names: list[str]) -> list[str]:
    """Replace namespace packages (``google``) by the real packages inside them.

    ``google-cloud-storage`` lists ``google`` in top_level.txt, but it only provides
    ``google.cloud.storage``; treating all of ``google`` as the package would attribute
    unrelated ``google.*`` imports to it.
    """
    out: list[str] = []
    for name in names:
        out.extend(_real_packages(root, name, depth=0))
    return sorted(set(out))


def _real_packages(root: Path, dotted: str, depth: int) -> list[str]:
    path = root.joinpath(*dotted.split("."))
    if not path.is_dir():
        return (
            [dotted]
            if path.with_suffix(".py").exists() or path.with_suffix(".pyi").exists()
            else []
        )
    if (path / "__init__.py").exists() or (path / "__init__.pyi").exists() or depth >= 3:
        return [dotted]
    if any(p.suffix in _SOURCE_SUFFIXES for p in path.iterdir() if p.is_file()):
        return [dotted]  # implicit namespace package with modules of its own
    found: list[str] = []
    for child in sorted(path.iterdir()):
        if child.is_dir() and not child.name.startswith((".", "_")):
            found.extend(_real_packages(root, f"{dotted}.{child.name}", depth + 1))
    return found


def _is_public_top(name: str) -> bool:
    return bool(name) and not name.startswith("_") and name.lower() not in _NON_PACKAGE_DIRS


def _sdist_package_root(tree: Path) -> Path:
    src = tree / "src"
    return src if src.is_dir() and any(src.glob("*/__init__.py")) else tree


def _import_names_from_tree(tree: Path) -> list[str]:
    names = [p.parent.name for p in tree.glob("*/__init__.py")]
    names += [p.stem for p in tree.glob("*.py") if p.stem not in {"setup", "conftest", "noxfile"}]
    return sorted(n for n in set(names) if _is_public_top(n))
