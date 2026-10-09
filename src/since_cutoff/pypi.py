"""PyPI access: release dates, "which version existed on date X", and source extraction.

Sources are taken from wheels (falling back to sdists) and only ``.py``/``.pyi`` files are
extracted, with path and size checks. Nothing is installed and no package code is executed.
"""

from __future__ import annotations

import ast
import contextlib
import email.parser
import hashlib
import io
import json
import re
import shutil
import tarfile
import tempfile
import threading
import time
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from since_cutoff import net
from since_cutoff.cache import DiskCache
from since_cutoff.errors import NoCodeError, PackageIndexError

PYPI_JSON = "https://pypi.org/pypi/{name}/json"
METADATA_TTL = 12 * 3600
# When PyPI cannot be reached, how long an older cached release list is used without asking
# PyPI again: long enough for one scan's many lookups of a package, short enough that a
# long-running process (the MCP server) tries again.
STALE_REUSE = 600
# 3: the marker lists the compiled modules that have no source or stub (SourceTree.compiled).
SOURCE_SCHEMA = 3
_SOURCE_SUFFIXES = (".py", ".pyi")
# Extension modules in a wheel: ``fast.cpython-312-x86_64-linux-gnu.so``, ``fast.abi3.so``,
# ``fast.cp312-win_amd64.pyd``, ``fast.pyd``. In an sdist, the Cython source (``fast.pyx``).
_EXTENSION_SUFFIXES = (".so", ".pyd")
_CYTHON_SUFFIXES = (".pyx",)
_NON_PACKAGE_DIRS = {"test", "tests", "docs", "doc", "examples", "example", "benchmarks", "scripts"}
_MAX_MEMBER_BYTES = 8 * 1024 * 1024
_MAX_TOTAL_BYTES = 512 * 1024 * 1024
_MAX_MEMBERS = 50_000
_MARKER = ".since-cutoff.json"
_MB = 1024 * 1024  # --max-download-mb counts in these, and so do the messages about it


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
    # Dotted names of the modules the distribution ships compiled, with no ``.py`` source and
    # no ``.pyi`` stub of the same name: a static reading sees nothing of them. A package whose
    # own ``__init__`` is compiled is ``pkg.__init__``.
    compiled: tuple[str, ...] = ()


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# What of PyPI's ``info`` the cache keeps: the summary and the URLs are for the notes
# (notes.new_package_note; changelog_url).
_INFO_KEPT = ("name", "version", "summary", "project_urls", "home_page")
# A project URL that documents the releases, by the label PyPI shows (lower case, spaces and
# hyphens dropped), in the order tried.
_CHANGELOG_LABELS = (
    "changelog",
    "changes",
    "releasenotes",
    "releases",
    "history",
    "whatsnew",
    "news",
)
_GITHUB_REPO = re.compile(
    r"^https?://(?:www\.)?github\.com/([^/\s]+)/([^/\s#?]+?)(?:\.git)?(?:[/?#].*)?$"
)


def changelog_url(urls: Mapping[str, str]) -> str | None:
    """Where a package documents its releases, from its project URLs (:meth:`PyPI.project_urls`):
    the URL labelled Changelog, Changes, Release notes, Releases, History, What's new or News,
    else the releases page of the GitHub repository another URL names
    (``https://github.com/owner/repo/releases``). None when neither is there."""
    by_label = {re.sub(r"[\s_-]+", "", k.lower()): v.strip() for k, v in urls.items()}
    for label in _CHANGELOG_LABELS:
        if by_label.get(label, "").startswith(("https://", "http://")):
            return by_label[label]
    for value in urls.values():
        m = _GITHUB_REPO.match(value.strip())
        if m and m.group(2).lower() not in ("issues", "discussions", "sponsors"):
            return f"https://github.com/{m.group(1)}/{m.group(2)}/releases"
    return None


class PyPI:
    def __init__(self, cache: DiskCache, *, max_download_mb: float = 80.0) -> None:
        self.cache = cache
        self.max_download_mb = max_download_mb
        self.max_download_bytes = int(max_download_mb * _MB)
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        # Release lists served from a cache entry older than METADATA_TTL because PyPI could
        # not be reached: canonical name -> (the entry, the day it was fetched, when it was
        # last tried). Reused for STALE_REUSE seconds, so that each later lookup in a scan does
        # not wait for the retries again.
        self._stale: dict[str, tuple[dict[str, Any], date, float]] = {}

    @property
    def stale(self) -> dict[str, date]:
        """The packages whose release list is an older cached copy because PyPI could not be
        reached, with the day each copy was fetched: releases published after it are unknown.
        A package leaves it once PyPI answers for it again.
        """
        with self._locks_guard:
            return {name: day for name, (_, day, _) in sorted(self._stale.items())}

    def _lock(self, key: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(key, threading.Lock())

    # ----------------------------------------------------------------- metadata
    def project(self, name: str) -> dict[str, Any]:
        """The package's PyPI JSON, cut down to what the scan reads: ``info`` (name, version,
        summary, project_urls, home_page) and ``releases``. Cached for METADATA_TTL; a cached
        copy from before the info held the URLs is fetched again."""
        key = canonicalize_name(name)
        cached = self.cache.get("pypi", key, max_age=METADATA_TTL)
        if cached is not None and "project_urls" not in (cached.get("info") or {}):
            cached = None
        if cached is not None:
            if self._stale:  # another process may have reached PyPI meanwhile
                with self._locks_guard:
                    self._stale.pop(key, None)
            return dict(cached)
        with self._locks_guard:
            stale = self._stale.get(key)
        if stale is not None and time.monotonic() - stale[2] < STALE_REUSE:
            return dict(stale[0])
        try:
            data = net.get_json(PYPI_JSON.format(name=key))
            slim = {
                "info": {k: data["info"].get(k) for k in _INFO_KEPT},
                "releases": dict(data.get("releases") or {}),
            }
        except net.HTTPError as exc:
            if exc.status == 404:
                raise PackageIndexError(f"'{name}' is not on PyPI") from exc
            fallback = self._stale_copy(key) if exc.transient else None
            if fallback is not None:
                return fallback
            raise PackageIndexError(f"could not reach PyPI for '{name}': {exc.reason}") from exc
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise PackageIndexError(f"PyPI returned an unexpected response for '{name}'") from exc
        self.cache.set("pypi", key, slim)
        with self._locks_guard:
            self._stale.pop(key, None)
        return slim

    def _stale_copy(self, key: str) -> dict[str, Any] | None:
        """The cached release list of ``key``, however old, for when PyPI cannot be reached;
        recorded in :attr:`stale`. None when there is none."""
        entry = self.cache.get("pypi", key)
        if not isinstance(entry, dict):
            return None
        try:
            fetched = self.cache.path("pypi", key).stat().st_mtime
        except OSError:
            return None
        day = datetime.fromtimestamp(fetched, tz=timezone.utc).date()
        with self._locks_guard:
            self._stale[key] = (entry, day, time.monotonic())
        return dict(entry)

    def summary(self, name: str) -> str | None:
        """PyPI's one-line summary of the package, from the cached metadata; None without."""
        text = (self.project(name).get("info") or {}).get("summary")
        return str(text).strip() or None if text else None

    def project_urls(self, name: str) -> dict[str, str]:
        """The package's project URLs on PyPI (``project_urls`` of its JSON; ``home_page`` as
        "Homepage" when the URLs do not name one), from the cached metadata. {} for a cached
        copy made before the metadata kept them, or a package that lists none."""
        info = self.project(name).get("info") or {}
        urls = {str(k): str(v) for k, v in (info.get("project_urls") or {}).items() if v}
        home = info.get("home_page")
        if home and not any(k.lower() == "homepage" for k in urls):
            urls["Homepage"] = str(home)
        return urls

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
        day = self.stale.get(canonicalize_name(name))
        if day is not None:  # it may well be on PyPI, published after the cached copy
            raise PackageIndexError(
                f"{name}=={version} is not in the cached PyPI metadata from {day.isoformat()}; "
                "PyPI could not be reached"
            )
        raise PackageIndexError(f"{name}=={version} is not on PyPI")

    def latest(self, name: str) -> Release:
        """The newest final, non-yanked release; for a package with only pre-releases, the
        newest of those, which is what pip and uv install then."""
        releases = [r for r in self.releases(name) if not r.yanked]
        finals = [r for r in releases if r.is_final]
        if not releases:
            raise PackageIndexError(f"'{name}' has no final releases on PyPI")
        return (finals or releases)[-1]

    def version_at(self, name: str, when: date) -> Release | None:
        """The highest final, non-yanked release that was published on or before ``when``; for
        a package that had only pre-releases by then (``0.0.1a5``, ``0.51b0``), the highest of
        those, since they were just as public. Never a development release: a lone
        ``0.0.1.dev5`` is how a name is reserved (nvidia-cuda-runtime), not an API."""
        return self.best_match(name, "", when) or self.best_match(
            name, "", when, prereleases=True, dev=False
        )

    def best_match(
        self,
        name: str,
        specifier: str,
        when: date | None = None,
        *,
        prereleases: bool = False,
        python: str | None = None,
        dev: bool = True,
    ) -> Release | None:
        """Newest final, non-yanked release matching ``specifier`` (and published by ``when``);
        with ``prereleases``, pre-releases and (unless ``dev`` is false) development releases
        count too. With ``python`` (``"3.11"``), only releases that support that Python count,
        as for pip."""
        try:
            spec = SpecifierSet(specifier, prereleases=prereleases or None)
        except InvalidSpecifier:
            spec = SpecifierSet(prereleases=prereleases or None)
        best: Release | None = None
        for r in self.releases(name):
            allowed = r.is_final or (prereleases and (dev or not r.parsed.is_devrelease))
            if not allowed or r.yanked or (when is not None and r.uploaded.date() > when):
                continue
            if r.parsed not in spec or (python and not _supports(r, python)):
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
            return SourceTree(name, version, root, *cached)

        rel = self.release(name, version)
        artifact = _pick_artifact(rel.files, allow_yanked=True)
        if artifact is None:
            raise PackageIndexError(f"{name}=={version} has no wheel or sdist on PyPI")
        size = int(artifact.get("size") or 0)
        if size > self.max_download_bytes:
            raise PackageIndexError(
                f"{name}=={version} is {size / _MB:.0f} MB, above the "
                f"{self.max_download_mb:g} MB download limit (--max-download-mb)"
            )
        filename = artifact["filename"]
        try:
            blob = net.request(artifact["url"], timeout=300, max_bytes=self.max_download_bytes + 1)
        except net.ResponseTooLarge as exc:  # PyPI understated its size
            raise PackageIndexError(
                f"{name}=={version} is above the {self.max_download_mb:g} MB download limit "
                "(--max-download-mb)"
            ) from exc
        except net.HTTPError as exc:
            raise PackageIndexError(f"could not download {filename}: {exc.reason}") from exc

        expected_sha256 = (artifact.get("digests") or {}).get("sha256")
        if expected_sha256:
            actual_sha256 = hashlib.sha256(blob).hexdigest()
            if actual_sha256.lower() != expected_sha256.lower():
                raise PackageIndexError(
                    f"hash mismatch for {filename}: expected {expected_sha256}, got {actual_sha256}"
                )

        sources.mkdir(parents=True, exist_ok=True)
        # A unique directory per extraction, published with an atomic rename, so concurrent
        # since-cutoff processes sharing the cache can never observe half-written trees.
        tmp = Path(tempfile.mkdtemp(dir=sources, prefix=f"{key}.tmp-"))
        try:
            try:
                if filename.endswith(".whl"):
                    files = _extract_zip(blob, tmp, strip_first=False)
                    package_root = tmp
                    dirs = _pth_dirs(blob)
                    lifted = _lift(tmp, dirs)
                    import_names = _wheel_import_names(blob, files, tmp, dirs, lifted)
                    requires = _requires_from_zip(blob, r"[^/]+\.dist-info/METADATA")
                    compiled = _compiled_modules(
                        [_on_path(f, dirs) for f in files], _EXTENSION_SUFFIXES
                    )
                else:
                    if filename.endswith(".zip"):
                        members = _extract_zip(blob, tmp / "_x", strip_first=True)
                        requires = _requires_from_zip(blob, r"[^/]+/PKG-INFO")
                    else:
                        members = _extract_tar(blob, tmp / "_x")
                        requires = _requires_from_tar(blob)
                    package_root = _sdist_package_root(tmp / "_x")
                    import_names = _refine_namespaces(
                        package_root, _import_names_from_tree(package_root)
                    )
                    # Members are named from the sdist's top directory; modules from its
                    # package root (``src/`` or the top directory itself).
                    prefix = package_root.relative_to(tmp / "_x").as_posix()
                    compiled = _compiled_modules(
                        [_under(m.split("/", 1)[-1], prefix) for m in members if "/" in m],
                        _CYTHON_SUFFIXES,
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
                meta = _metapackage(name, version, requires)
                if meta:
                    raise PackageIndexError(meta)
                raise NoCodeError(f"could not find importable modules in {filename}")
            (package_root / _MARKER).write_text(
                json.dumps(
                    {
                        "schema": SOURCE_SCHEMA,
                        "import_names": import_names,
                        "requires": requires,
                        "compiled": compiled,
                    }
                ),
                encoding="utf-8",
            )
            try:
                package_root.rename(root)
            except OSError:
                if _read_marker(root) is None:  # a stale or broken tree: replace it
                    shutil.rmtree(root, ignore_errors=True)
                    # Unless another process replaced it first, or the rename is refused (a
                    # virus scanner holding a file): the marker below tells which.
                    with contextlib.suppress(OSError):
                        package_root.rename(root)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        cached = _read_marker(root)
        if cached is None:
            raise PackageIndexError(f"could not publish the sources of {name}=={version}")
        return SourceTree(name, version, root, *cached)


# --------------------------------------------------------------------- helpers
def is_placeholder(tree: SourceTree, *, max_files: int = 20) -> bool:
    """Whether a release only reserves its name: its modules define nothing but a docstring
    and dunder values (``__version__``), like zensical 0.0.0's empty ``__init__.py``."""
    files: list[Path] = []
    for name in tree.import_names:
        path = tree.root.joinpath(*name.split("."))
        if path.is_dir():
            files += [f for f in path.rglob("*") if f.suffix in _SOURCE_SUFFIXES]
        else:
            files += [f for f in (path.with_suffix(".py"), path.with_suffix(".pyi")) if f.is_file()]
        if len(files) > max_files:
            return False
    for f in files:
        try:
            body = ast.parse(f.read_text(encoding="utf-8", errors="replace")).body
        except (OSError, SyntaxError, ValueError, RecursionError, MemoryError):
            return False
        for node in body:
            if isinstance(node, ast.Pass) or (
                isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
            ):
                continue
            targets = (
                node.targets
                if isinstance(node, ast.Assign)
                else [node.target]
                if isinstance(node, ast.AnnAssign)
                else []
            )
            if not targets or not all(
                isinstance(t, ast.Name) and t.id.startswith("__") for t in targets
            ):
                return False
    return bool(files)


def _supports(release: Release, python: str) -> bool:
    """Whether some file of a release installs on ``python`` (its ``Requires-Python``)."""
    try:
        version = Version(python)
    except InvalidVersion:
        return True
    for f in release.files:
        try:
            if version in SpecifierSet(str(f.get("requires_python") or "")):
                return True
        except InvalidSpecifier:
            return True
    return not release.files


def _metapackage(name: str, version: str, requires: list[str]) -> str | None:
    """Why a release without modules has nothing to diff, when it is a metapackage (docling,
    griffe 2): no code of its own, only requirements on the packages that have it."""
    installs = []
    for line in requires:
        try:
            req = Requirement(line)
        except InvalidRequirement:
            continue
        if req.marker is None or "extra" not in str(req.marker):
            installs.append(f"{req.name}{req.specifier}")
    if not installs:
        return None
    what = "that package" if len(installs) == 1 else "those packages"
    return (
        f"{name} {version} is a metapackage without code of its own: it installs "
        f"{', '.join(installs[:3])}{', ...' if len(installs) > 3 else ''}; check {what} instead"
    )


def _read_marker(
    root: Path,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]] | None:
    """``(import_names, requires, compiled)`` of an extracted tree, or None when it must be
    extracted again (no marker, an earlier SOURCE_SCHEMA, or names an earlier version got
    wrong)."""
    try:
        data = json.loads((root / _MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema") != SOURCE_SCHEMA:
        return None
    names = [str(n) for n in data.get("import_names") or ()]
    if any("\\" in n for n in names):
        return None  # pywin32's paths, extracted before its .pth directories were followed
    names = _refine_namespaces(root, names) or names  # trees extracted by earlier versions
    compiled = tuple(str(n) for n in data.get("compiled") or ())
    return _stub_names(names), tuple(data.get("requires") or ()), compiled


def _stub_names(names: list[str]) -> tuple[str, ...]:
    """Import names with a stub-only package's ``foo-stubs`` directory as ``foo`` (PEP 561):
    code imports pandas, not pandas-stubs (see :func:`since_cutoff.apidiff.load_api`)."""
    return tuple(dict.fromkeys(n.removesuffix("-stubs") for n in names))


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


def _safe_target(dest: Path, member: str, resolved: dict[Path, Path] | None = None) -> Path | None:
    """Map an archive member to a path inside ``dest``, or None if it would escape it.

    ``resolved`` keeps the directories already resolved during one extraction: resolving
    every member's path took half the time of extracting a wheel on Windows.
    """
    name = member.replace("\\", "/")
    parts = PurePosixPath(name).parts
    # Windows drops a trailing dot or space from a name ("pkg." is "pkg") or refuses it, and
    # cannot delete a "..." or ".. " it made: no module or package is named like that.
    if (
        not parts
        or PurePosixPath(name).is_absolute()
        or PureWindowsPath(name).drive
        or any(p in ("", ".", "..") or ":" in p or p.endswith((".", " ")) for p in parts)
    ):
        return None
    target = dest.joinpath(*parts)
    cache = resolved if resolved is not None else {}
    for path in (dest, target.parent):
        if path not in cache:
            cache[path] = path.resolve()
    try:
        (cache[target.parent] / target.name).relative_to(cache[dest])
    except ValueError:
        return None
    return target


def _wanted(member: str) -> bool:
    return member.endswith(_SOURCE_SUFFIXES) or member.endswith("py.typed")


def _compiled_modules(members: list[str], suffixes: tuple[str, ...]) -> list[str]:
    """Dotted names of the compiled modules among an archive's ``members`` (paths from where
    they are imported) that have no ``.py`` source and no ``.pyi`` stub of the same name.

    Only sources are extracted, so without this list a public module that became compiled
    (``pkg/fast.py`` -> ``pkg/fast.cpython-312-x86_64-linux-gnu.so``) looked removed.
    ``suffixes`` are the compiled forms: extension modules in a wheel, Cython sources in an
    sdist. A package whose ``__init__`` is compiled is ``pkg.__init__``: its submodules still
    have files of their own. A file whose path does not spell a module
    (``numpy.libs/libopenblas.so``, a shared library a wheel vendors) is none, nor is mypyc's
    helper module (``<hash>__mypyc.cpython-312-x86_64-linux-gnu.so``), which no code imports.
    """
    readable: set[str] = set()
    compiled: dict[str, str] = {}
    for member in members:
        path = PurePosixPath(member.replace("\\", "/"))
        parts = [*path.parent.parts, path.name.split(".", 1)[0]]
        if not all(p.isidentifier() for p in parts) or parts[-1].endswith("__mypyc"):
            continue
        key = "/".join(parts)
        if path.suffix in _SOURCE_SUFFIXES:
            readable.add(key)
        elif path.suffix in suffixes:
            compiled[key] = ".".join(parts)
    return sorted(name for key, name in compiled.items() if key not in readable)


def _under(member: str, prefix: str) -> str:
    """``member`` relative to ``prefix`` (a directory, or "" for the top), or "" when it is
    outside it."""
    if prefix in ("", "."):
        return member
    return member[len(prefix) + 1 :] if member.startswith(prefix + "/") else ""


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
    resolved: dict[Path, Path] = {}
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename
            names.append(name)
            if not _wanted(name) or info.file_size > _MAX_MEMBER_BYTES:
                continue
            rel = name.split("/", 1)[1] if strip_first and "/" in name else name
            target = _safe_target(dest, rel, resolved)
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
    resolved: dict[Path, Path] = {}
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
            target = _safe_target(dest, member.name.split("/", 1)[1], resolved)
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


def _read_member(zf: zipfile.ZipFile, name: str, limit: int) -> str:
    """The first ``limit`` bytes of a metadata file in an archive, as text: however far the
    member expands, no more of it is decompressed."""
    with zf.open(name) as fh:
        return fh.read(limit).decode("utf-8", "replace")


def _parse_requires(metadata: str) -> list[str]:
    msg = email.parser.Parser().parsestr(metadata, headersonly=True)
    return [r.strip() for r in msg.get_all("Requires-Dist") or [] if r.strip()]


def _requires_from_zip(blob: bytes, pattern: str) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        for n in zf.namelist():
            if re.fullmatch(pattern, n):
                return _parse_requires(_read_member(zf, n, 1_000_000))
    return []


def _requires_from_tar(blob: bytes) -> list[str]:
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:*") as tf:
        for member in tf:
            if member.isfile() and re.fullmatch(r"[^/]+/PKG-INFO", member.name):
                fh = tf.extractfile(member)
                if fh is not None:
                    return _parse_requires(fh.read(1_000_000).decode("utf-8", "replace"))
    return []


def _pth_dirs(blob: bytes) -> list[str]:
    """The directories a wheel's ``.pth`` files put on ``sys.path``, in order: pywin32 adds
    ``win32``, ``win32/lib`` and ``Pythonwin``, so ``win32/lib/win32con.py`` is ``win32con``."""
    dirs: list[str] = []
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        for name in zf.namelist():
            if "/" in name or not name.endswith(".pth"):
                continue
            for line in _read_member(zf, name, 100_000).splitlines():
                line = line.strip().replace("\\", "/").strip("/")
                if line and not line.startswith(("#", "import ", "import\t")) and ".." not in line:
                    dirs.append(line)
    return dirs


def _lift(root: Path, dirs: list[str]) -> list[str]:
    """Move the modules and packages in ``dirs`` (see :func:`_pth_dirs`) to ``root``, where
    they are imported from; the first directory wins, as on ``sys.path``. Returns their names
    (top_level.txt may list only the directory: pywin32 311 lists ``pythonwin``)."""
    lifted: list[str] = []
    for d in dirs:
        base = root
        for part in d.split("/"):  # the .pth says "Pythonwin", the wheel has "pythonwin"
            base = next((c for c in _children(base) if c.name.lower() == part.lower()), base / part)
        for child in _children(base):
            package = child.is_dir() and any(
                (child / f"__init__{s}").is_file() for s in _SOURCE_SUFFIXES
            )
            if (package or child.suffix in _SOURCE_SUFFIXES) and not (root / child.name).exists():
                child.rename(root / child.name)
                lifted.append(child.stem if child.is_file() else child.name)
    return lifted


def _children(path: Path) -> list[Path]:
    return sorted(path.iterdir()) if path.is_dir() else []


def _on_path(name: str, dirs: list[str]) -> str:
    """``win32/lib/win32con`` -> ``win32con`` when a .pth puts ``win32/lib`` on sys.path."""
    for d in sorted(dirs, key=len, reverse=True):
        if name.lower().startswith(d.lower() + "/"):
            return name[len(d) + 1 :]
    return name


def _wheel_import_names(
    blob: bytes,
    files: list[str],
    root: Path,
    dirs: list[str] | None = None,
    lifted: list[str] | None = None,
) -> list[str]:
    names: list[str] = []
    on_path = {d.lower() for d in dirs or []}  # directories, not packages
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        top = [n for n in zf.namelist() if re.match(r"[^/]+\.dist-info/top_level\.txt$", n)]
        if top:
            listed = [
                _on_path(line.strip().replace("\\", "/"), dirs or [])
                for line in _read_member(zf, top[0], 1_000_000).splitlines()
            ]
            names = [n.replace("/", ".") for n in listed if _is_public_top(n)]
            names = [n for n in names if n.lower() not in on_path]
            names += [n for n in lifted or [] if _is_public_top(n)]
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
    modules = sorted(
        {p.stem for p in path.iterdir() if p.is_file() and p.suffix in _SOURCE_SUFFIXES}
    )
    if modules and "." not in dotted:
        return [dotted]  # implicit namespace package with modules of its own
    found: list[str] = []
    if modules:
        # A namespace inside a namespace, with modules of its own: google-cloud-core's
        # google/cloud/client.py. Other distributions share google.cloud (bigquery,
        # storage), so this one is its own modules, not all of google.cloud.
        found = [f"{dotted}.{m}" for m in modules if not m.startswith("_")]
        if not found:
            return [dotted]
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
    names = [p.parent.name for p in [*tree.glob("*/__init__.py"), *tree.glob("*/__init__.pyi")]]
    names += [p.stem for p in tree.glob("*.py") if p.stem not in {"setup", "conftest", "noxfile"}]
    return sorted(n for n in set(names) if _is_public_top(n))
