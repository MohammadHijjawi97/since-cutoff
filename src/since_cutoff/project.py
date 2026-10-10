"""Discover a Python project's dependencies and the exact versions it uses.

Version sources, most authoritative first: a lockfile (uv.lock, poetry.lock, pdm.lock,
pylock.toml, Pipfile.lock), the project's virtual environment, pinned requirement files, and
finally "latest release on PyPI" for anything that is declared but not pinned.

Dependencies that do not come from PyPI (git, local paths, workspace members, private indexes)
are recorded but never looked up on PyPI by name.
"""

from __future__ import annotations

import ast
import builtins
import codecs
import json
import re
import sys
import warnings
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, NamedTuple

from packaging.markers import (
    InvalidMarker,
    Marker,
    UndefinedComparison,
    UndefinedEnvironmentName,
    default_environment,
)
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from since_cutoff.errors import ProjectError

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

SKIP_DIRS = {
    ".git",
    ".hg",
    ".venv",
    "venv",
    "env",
    ".env",
    "node_modules",
    "build",
    "dist",
    "site-packages",
    "__pycache__",
    ".tox",
    ".nox",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    ".since-cutoff",
}
LOCKFILES = ("uv.lock", "poetry.lock", "pdm.lock", "pylock.toml", "Pipfile.lock")
VENV_DIRS = (".venv", "venv", "env", ".env")
PYPI_HOSTS = ("pypi.org", "files.pythonhosted.org", "pypi.python.org")
# The version source of a project where nothing pins a version: the scan takes each
# dependency's latest release on PyPI.
LATEST = "latest on PyPI, as nothing is pinned"
# How a non-PyPI source reads after "installed from" (Dependency.non_pypi). A direct reference
# (``pkg @ git+https://...``) is its URL.
_SOURCE_TEXT = {
    "path": "a local path",
    "directory": "a local directory",
    "file": "a local file",
    "editable": "an editable install",
    "virtual": "a workspace member",
    "workspace": "a workspace member",
    "url": "a URL",
    "archive": "an archive URL",
    "vcs": "version control",
    "private index": "a private index",
    "local or git": "a local path or git",
}


@dataclass
class Dependency:
    name: str
    version: str | None
    direct: bool
    source: str
    non_pypi: str | None = None  # why it must not be looked up on PyPI (git, path, index, ...)
    # The range the project declares for a version nothing pins (``>=4.54,<5``): the scan
    # takes the newest release in it. Empty when any version goes.
    specifier: str = ""
    # The file that pins it, for a version from the project's own files (source "pinned"):
    # ``pyproject.toml``, ``requirements.txt``.
    pinned_in: str | None = None

    @property
    def key(self) -> str:
        return canonicalize_name(self.name)

    @property
    def non_pypi_reason(self) -> str | None:
        """Why the scan skips this dependency: ``installed from git, not from PyPI``."""
        if not self.non_pypi:
            return None
        return f"installed from {_SOURCE_TEXT.get(self.non_pypi, self.non_pypi)}, not from PyPI"


@dataclass
class Project:
    root: Path
    dependencies: list[Dependency]
    version_source: str
    python_version: str | None = None
    imported_modules: set[str] = field(default_factory=set)
    # What each file of the project's code reaches through its imports (scan_sources).
    files: list[FileUse] = field(default_factory=list)
    # Whether a lockfile or an environment lists the transitive dependencies too; declared
    # dependencies alone (pyproject.toml, requirements, Pipfile) name only the direct ones.
    transitive: bool = True
    # The Python given with --python or in .python-version, if any: the one environment
    # markers, uv.lock forks and the versions of unpinned dependencies are resolved for.
    # Without it they are resolved for no particular Python (the newest version wins).
    resolution_python: str | None = None
    # What the scan should say about where the versions come from (a lockfile it ignored).
    warnings: list[str] = field(default_factory=list)
    # Every distribution the project's virtual environment has, by canonical name (its
    # ``*.dist-info``): ``dependencies`` lists the declared and locked ones only when there is
    # no lockfile, and a transitive one may matter too (engine.ScanResult.installed).
    installed: dict[str, str] = field(default_factory=dict)

    def direct(self) -> list[Dependency]:
        return [d for d in self.dependencies if d.direct]

    @property
    def versions_word(self) -> str:
        """Where the versions come from, for a sentence: the lockfile or the file that pins
        them (``uv.lock``, ``pyproject.toml``), "your virtual environment", or "your
        dependencies" when PyPI's latest releases stand in for pins."""
        first = self.version_source.split(",")[0].strip()
        if not first or first.startswith("latest on PyPI"):
            return "your dependencies"
        return "your virtual environment" if first in VENV_DIRS else first

    def imports(self, import_names: Iterable[str]) -> bool:
        """Whether the project's code imports any of a distribution's import names.

        A name inside a namespace package (``google.cloud.storage``) counts only when that
        package or a module in it is imported: ``from google import genai`` imports
        google-genai, not google-cloud-storage.
        """
        names = tuple(import_names)
        return any(f.imports(names) for f in self.files)

    def code_use(self, import_names: Iterable[str]) -> tuple[FileUse, ...]:
        """The files of the project's code that import any of a distribution's import names
        (as :meth:`imports` counts them), or that hold a value whose class derives from one of
        the distribution's classes without importing it (FileUse.reaches: ``llm =
        ChatOpenAI(...)`` from langchain-openai is a langchain-core ``BaseChatModel``, so the
        file uses langchain-core's API too)."""
        names = tuple(import_names)
        return tuple(f for f in self.files if f.imports(names) or f.reaches_any(names))


@dataclass
class _Lock:
    versions: dict[str, str] = field(default_factory=dict)
    direct: set[str] = field(default_factory=set)
    non_pypi: dict[str, str] = field(default_factory=dict)
    members: set[str] = field(default_factory=set)


# ------------------------------------------------------------------ public API
def load_project(root: Path, *, python: str | None = None) -> Project:
    root = root.resolve()
    if not root.is_dir():
        raise ProjectError(f"{root} is not a directory")

    pv = python or python_version(root, explicit_only=True)
    found = _declared_dependencies(root, pv)
    declared, pinned_in, ranges = found.versions, found.pinned_in, found.ranges
    workspace = {name for name, reason in found.local.items() if reason == "workspace"}
    local = {name: reason for name, reason in found.local.items() if name not in workspace}
    lock = _Lock()
    version_source = ""
    notices: list[str] = []
    lockfile = next((root / n for n in LOCKFILES if (root / n).is_file()), None)
    if lockfile is None:
        pylocks = sorted(root.glob("pylock.*.toml"))
        lockfile = pylocks[0] if pylocks else None
    if lockfile is not None:
        lock = parse_lock(lockfile, python=pv)
        version_source = lockfile.name
        clash = _pin_conflicts(declared, lock.versions)
        if clash:
            sample = ", ".join(f"{k} {lock.versions[k]} vs =={declared[k]}" for k in clash[:3])
            sample += f", and {len(clash) - 3} more" if len(clash) > 3 else ""
            if not _lock_in_use(root, lockfile):
                # A lockfile left over from a tool the project no longer uses (a poetry.lock
                # next to pip-compile's requirements.txt) must not override what it pins.
                notices.append(
                    f"ignored {lockfile.name}: the project is not set up for "
                    f"{_LOCK_TOOLS[lockfile.name]}, and {len(clash)} of its versions disagree "
                    f"with the pinned requirements ({sample})"
                )
                lock, version_source = _Lock(), ""
            else:
                notices.append(
                    f"{len(clash)} versions in {lockfile.name} disagree with the pinned "
                    f"requirements ({sample}); the versions from {lockfile.name} are checked"
                )

    installed = installed_versions(root)
    if not lock.versions and installed:
        version_source = next(d for d in VENV_DIRS if (root / d).is_dir())

    lock.members |= workspace
    direct_names = ((set(declared) - found.indirect) | lock.direct) - lock.members
    if not direct_names and lock.versions:
        direct_names = set(lock.versions)
    if not direct_names and installed:
        direct_names = set(installed)
    if not direct_names and not local:
        raise ProjectError(_no_dependencies(root))

    deps: list[Dependency] = []
    for key in sorted(
        (set(lock.versions) | set(declared) | direct_names | set(local)) - lock.members
    ):
        version, source = None, ""
        if key in lock.versions:
            version, source = lock.versions[key], version_source
        elif key in installed:
            version, source = installed[key], "installed"
        elif declared.get(key):
            version, source = declared[key], "pinned"
        non_pypi = lock.non_pypi.get(key) or local.get(key)
        direct = key in direct_names or key in local
        specifier = "" if version else ranges.get(key, "")
        where = pinned_in.get(key) if source == "pinned" else None
        deps.append(
            Dependency(key, version, direct, source or "unpinned", non_pypi, specifier, where)
        )

    sources = scan_sources(root)
    if version_source:
        version_source = _mixed_source(version_source, deps, pinned_in)
    return Project(
        root=root,
        dependencies=deps,
        version_source=version_source or _declared_source(deps, pinned_in),
        python_version=python_version(root),
        imported_modules=sources.modules,
        files=sources.files,
        transitive=bool(lock.versions or installed or found.compiled),
        resolution_python=pv,
        warnings=notices,
        installed=installed,
    )


def _declared_source(deps: list[Dependency], pinned_in: dict[str, str]) -> str:
    """Where the versions come from without a lockfile or environment: the files that pin
    them (``requirements.txt``), and PyPI's latest release for the rest."""
    files = sorted({pinned_in[d.key] for d in deps if d.source == "pinned" and d.key in pinned_in})
    unpinned = sum(d.version is None and not d.non_pypi for d in deps)
    if not files:
        return LATEST
    if not unpinned:
        return ", ".join(files)
    return f"{', '.join(files)}, latest on PyPI for {unpinned} unpinned"


def _mixed_source(main: str, deps: list[Dependency], pinned_in: dict[str, str]) -> str:
    """The version source of a project with a lockfile or environment (``main``), and where
    the versions of the dependencies it does not list come from: ``poetry.lock,
    requirements.txt for 32 not in it``."""
    own = "installed" if main in VENV_DIRS else main
    rest = [d for d in deps if d.source != own and not d.non_pypi]
    pinned = [d for d in rest if d.source == "pinned"]
    installed = sum(d.source == "installed" for d in rest)
    unpinned = sum(d.version is None for d in rest)
    parts = [main]
    if installed:
        parts.append(f"the virtual environment for {installed} not in it")
    if pinned:
        files = sorted({pinned_in.get(d.key, "requirements") for d in pinned})
        parts.append(f"{', '.join(files)} for {len(pinned)} not in it")
    if unpinned:
        parts.append(f"latest on PyPI for {unpinned} unpinned")
    return ", ".join(parts)


def _no_dependencies(root: Path) -> str:
    """The error for a project without dependencies it can read, naming what it can read and
    the setup.py or setup.cfg it does not (running setup.py would run the project's code)."""
    text = (
        f"no dependencies found in {root}. since-cutoff reads pyproject.toml, requirements*.txt, "
        "Pipfile, a lockfile (uv.lock, poetry.lock, pdm.lock, pylock.toml, Pipfile.lock) or a "
        "virtual environment (.venv)."
    )
    legacy = [name for name in ("setup.py", "setup.cfg") if (root / name).is_file()]
    if legacy:
        text += (
            f" It does not read {' or '.join(legacy)}: list the dependencies in "
            "requirements.txt or pyproject.toml, or install the project into a .venv."
        )
    return text


# ---------------------------------------------------------------- lockfiles
# The tool whose own settings a lockfile needs (see _lock_in_use).
_LOCK_TOOLS = {"poetry.lock": "Poetry", "pdm.lock": "PDM", "Pipfile.lock": "Pipenv"}


def _pin_conflicts(declared: dict[str, str | None], locked: dict[str, str]) -> list[str]:
    """The dependencies pinned with ``==`` in the project's own files whose locked version is
    another one."""
    out = []
    for name, pin in sorted(declared.items()):
        if pin and name in locked:
            try:
                same = Version(pin) == Version(locked[name])
            except InvalidVersion:
                same = pin == locked[name]
            if not same:
                out.append(name)
    return out


def _lock_in_use(root: Path, lockfile: Path) -> bool:
    """Whether the project is set up for the tool that wrote ``lockfile``.

    uv.lock and pylock.toml go with any standard ``[project]``; poetry.lock and pdm.lock need
    their tool's table or build backend in pyproject.toml, and Pipfile.lock needs a Pipfile.
    """
    if lockfile.name == "Pipfile.lock":
        return (root / "Pipfile").is_file()
    tool = {"poetry.lock": "poetry", "pdm.lock": "pdm"}.get(lockfile.name)
    if tool is None:
        return True
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return False
    try:
        data = _load_toml(pyproject)
    except ProjectError:
        return True
    build = _table(data.get("build-system"))
    backend = str(build.get("build-backend") or "")
    requires = " ".join(str(r) for r in _array(build.get("requires"))).lower()
    return tool in _table(data.get("tool")) or backend.startswith(tool) or tool in requires


def _read_text(path: Path) -> str:
    """Decode a text file the way pip does: honour BOMs (UTF-8/16/32), default to UTF-8."""
    data = path.read_bytes()
    for bom, enc in (
        (codecs.BOM_UTF32_LE, "utf-32"),
        (codecs.BOM_UTF32_BE, "utf-32"),
        (codecs.BOM_UTF8, "utf-8-sig"),
        (codecs.BOM_UTF16_LE, "utf-16"),
        (codecs.BOM_UTF16_BE, "utf-16"),
    ):
        if data.startswith(bom):
            return data.decode(enc, errors="replace")
    return data.decode("utf-8", errors="replace")


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        data: dict[str, Any] = tomllib.loads(_read_text(path))
        return data
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ProjectError(f"could not parse {path.name}: {exc}") from exc


# A hand-edited or damaged file can hold anything where a table or a list belongs: that part is
# read as missing. ``dependencies = "requests"`` is not six one-letter dependencies.
def _table(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _array(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _dependency_names(entries: object) -> list[str]:
    """The names in a list of ``{name = "..."}`` tables (uv.lock's dependencies)."""
    return [
        canonicalize_name(e["name"])
        for e in _array(entries)
        if isinstance(e, dict) and isinstance(e.get("name"), str)
    ]


def parse_lockfile(path: Path) -> tuple[dict[str, str], set[str]]:
    """Return ``({name: version}, direct_dependency_names)`` for any supported lockfile."""
    lock = parse_lock(path)
    return lock.versions, lock.direct


def parse_lock(path: Path, *, python: str | None = None) -> _Lock:
    if path.name == "Pipfile.lock":
        return _parse_pipfile_lock(path)
    data = _load_toml(path)
    if path.name == "uv.lock":
        return _parse_uv_lock(data, python)
    lock = _Lock()
    candidates: dict[str, list[tuple[Version, str, list[str]]]] = {}
    entries = data.get("packages") if path.name.startswith("pylock") else data.get("package")
    for p in _array(entries):
        if not isinstance(p, dict) or not isinstance(p.get("name"), str) or not p.get("version"):
            continue
        name = canonicalize_name(p["name"])
        version = str(p["version"])
        reason = _non_pypi_reason(p)
        if reason:
            lock.non_pypi[name] = reason
        try:
            parsed = Version(version)
        except InvalidVersion:
            lock.versions[name] = version
            continue
        # A package locked once per Python range (Poetry's ``markers``, per dependency group
        # too; PDM's and pylock's ``marker``) is a fork, as in uv.lock.
        marker = p.get("markers", p.get("marker"))
        markers = list(marker.values()) if isinstance(marker, dict) else [marker]
        candidates.setdefault(name, []).append(
            (parsed, version, [m for m in markers if isinstance(m, str)])
        )
    for locked_name, forks in candidates.items():
        lock.versions[locked_name] = _pick_forked(forks, python)
    return lock


def _non_pypi_reason(entry: dict[str, Any]) -> str | None:
    """Why a locked package must not be resolved against public PyPI, if at all."""
    source = entry.get("source")
    if isinstance(source, dict):  # uv.lock and poetry.lock
        kind = str(source.get("type") or "")
        if "git" in source or kind == "git":
            return _git_source(source)
        if "url" in source and kind in ("", "url"):  # uv's {url = ...}, Poetry's type = "url"
            return _public_url(str(source["url"]))
        for key in ("path", "directory", "editable", "virtual"):
            if key in source:
                return key
        registry = str(source.get("registry") or source.get("url") or "")
        if kind and kind not in ("pypi", ""):
            return kind if kind != "legacy" else "private index"
        if registry and not any(h in registry for h in PYPI_HOSTS):
            return "private index"
    if isinstance(entry.get("git"), str):  # pdm
        return _git_source(entry)
    for key in ("git", "path", "vcs", "directory", "archive", "url"):  # pdm / pylock
        if key in entry:
            return key
    index = str(entry.get("index") or "")
    if index and not any(h in index for h in PYPI_HOSTS):
        return "private index"
    return None


def _git_source(source: dict[str, Any]) -> str:
    """A locked git source as a direct reference: ``git+https://github.com/org/pkg@7fc59da2c4df``
    from uv's ``{git = "https://...#<commit>"}``, Poetry's ``url`` and ``resolved_reference``
    or pdm's ``git`` and ``revision``."""
    if isinstance(source.get("git"), str):
        url, _, commit = str(source["git"]).partition("#")
        url = url.split("?", 1)[0]
        commit = commit or str(source.get("revision") or "")
    else:
        url = str(source.get("url") or "")
        commit = str(source.get("resolved_reference") or source.get("reference") or "")
    return f"git+{_public_url(url)}" + (f"@{commit[:12]}" if commit else "")


def _parse_uv_lock(data: dict[str, Any], python: str | None) -> _Lock:
    lock = _Lock()
    candidates: dict[str, list[tuple[Version, str, list[str]]]] = {}
    for pkg in _array(data.get("package")):
        if not isinstance(pkg, dict) or not isinstance(pkg.get("name"), str):
            continue
        name = canonicalize_name(pkg["name"])
        source = _table(pkg.get("source"))
        # Editable/virtual sources are the project itself (or workspace members): their
        # dependencies are the project's direct dependencies.
        if "editable" in source or "virtual" in source:
            lock.members.add(name)
            lock.direct.update(_dependency_names(pkg.get("dependencies")))
            for group in _table(pkg.get("optional-dependencies")).values():
                lock.direct.update(_dependency_names(group))
            for group in _table(pkg.get("dev-dependencies")).values():
                lock.direct.update(_dependency_names(group))
            continue
        version = str(pkg.get("version") or "")
        if not version:
            continue
        reason = _non_pypi_reason(pkg)
        if reason:
            lock.non_pypi[name] = reason
        try:
            parsed = Version(version)
        except InvalidVersion:
            lock.versions[name] = version
            continue
        markers = [m for m in _array(pkg.get("resolution-markers")) if isinstance(m, str)]
        candidates.setdefault(name, []).append((parsed, version, markers))
    for locked_name, entries in candidates.items():
        lock.versions[locked_name] = _pick_forked(entries, python)
    lock.direct -= lock.members
    return lock


# What evaluating a marker can raise. InvalidVersion: packaging before 26 compares
# ``platform_release >= '5'`` as versions, and Linux's "6.5.0-1025-azure" is none.
_MARKER_ERRORS = (InvalidMarker, InvalidVersion, UndefinedComparison, UndefinedEnvironmentName)


def _pick_forked(entries: list[tuple[Version, str, list[str]]], python: str | None) -> str:
    """A lockfile can lock one version per environment fork (uv's ``resolution-markers``,
    Poetry's and PDM's markers); pick the newest one for the project's Python on any of the
    platforms (_PLATFORMS), not only on the one the scan runs on."""
    if len(entries) == 1 or python is None:
        return max(entries)[1]
    env = {"python_version": python, "python_full_version": f"{python}.0"}
    matching = []
    for parsed, raw, markers in entries:
        try:
            ok = not markers or any(
                Marker(m).evaluate({**env, **platform}) for m in markers for platform in _PLATFORMS
            )
        except _MARKER_ERRORS:
            ok = True
        if ok:
            matching.append((parsed, raw, markers))
    return max(matching or entries)[1]


def _parse_pipfile_lock(path: Path) -> _Lock:
    try:
        data = json.loads(_read_text(path))
    except (OSError, ValueError) as exc:
        raise ProjectError(f"could not parse {path.name}: {exc}") from exc
    if not isinstance(data, dict):
        raise ProjectError(f"could not parse {path.name}: not a JSON object")
    lock = _Lock()
    for section in ("default", "develop"):
        for name, spec in _table(data.get(section)).items():
            if not isinstance(spec, dict):
                continue
            key = canonicalize_name(name)
            for k in ("git", "path", "file", "editable"):
                if k in spec:
                    lock.non_pypi[key] = k
            v = str(spec.get("version", ""))
            if v.startswith("=="):
                lock.versions[key] = v[2:]
    return lock


# ------------------------------------------------------ declared dependencies
class _Declared(NamedTuple):
    versions: dict[str, str | None]  # name -> pinned version, or None
    local: dict[str, str]  # name -> why it is not looked up on PyPI
    pinned_in: dict[str, str]  # name -> the file that pins it
    ranges: dict[str, str]  # name -> the declared range of an unpinned one
    # Names that only pip-compile output lists, as dependencies of other packages.
    indirect: set[str]
    # Whether a requirements file is pip-compile output, which pins the whole tree.
    compiled: bool


def _declared_dependencies(root: Path, python: str | None = None) -> _Declared:
    """The dependencies the project's own files declare.

    A name can be declared several times: in extras, groups and files, for some Pythons or
    platforms only. The declarations whose environment markers can hold for ``python`` (the
    newest Python when None) count, or all of them when none can; of those, the newest pin
    wins, a name without a pin gets the range they all allow, and a name is not looked up on
    PyPI only when every one of them is a direct reference (``torch @ https://...``). A name
    that only pip-compile's output lists, ``# via`` other packages, is not a direct one.
    """
    found: dict[str, list[tuple[Requirement, str]]] = {}
    local: dict[str, str] = {}
    direct: set[str] = set()
    compiled = False

    def add(req: Requirement, file: str, is_direct: bool = True) -> None:
        found.setdefault(canonicalize_name(req.name), []).append((req, file))
        if is_direct:
            direct.add(canonicalize_name(req.name))

    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        data = _load_toml(pyproject)
        project_name = canonicalize_name(str(_table(data.get("project")).get("name", "")))
        for name, reason in _local_sources(data).items():
            local[name] = reason
        for req in _pyproject_requirements(data):
            if canonicalize_name(req.name) != project_name:
                add(req, pyproject.name)

    pipfile = root / "Pipfile"
    if pipfile.is_file():
        try:
            data = _load_toml(pipfile)
            for section in ("packages", "dev-packages"):
                for name, spec in _table(data.get(section)).items():
                    if _is_root(root, spec):
                        # The project itself, installed for its tests (Pipenv names it after a
                        # hash: ``e1839a8 = {path = ".", editable = true}``), is not a dependency.
                        continue
                    if isinstance(spec, dict) and spec.keys() & {"git", "path", "file", "editable"}:
                        local[canonicalize_name(name)] = "local or git"
                    else:
                        entry = _pipfile_requirement(name, spec)
                        if entry is not None:
                            add(entry, pipfile.name)
        except ProjectError:
            pass

    for req_file in _requirement_files(root):
        roles = _compiled_roles(req_file)
        compiled = compiled or roles is not None
        for req in parse_requirements(req_file):
            is_direct = roles is None or roles.get(canonicalize_name(req.name), True)
            add(req, req_file.relative_to(root).as_posix(), is_direct)

    out: dict[str, str | None] = {}
    pinned_in: dict[str, str] = {}
    ranges: dict[str, str] = {}
    for key, declarations in found.items():
        if key in local:  # [tool.uv.sources], Poetry or the Pipfile point it elsewhere
            continue
        applying = [(r, f) for r, f in declarations if _applies(r, python)] or declarations
        on_pypi = [(r, f) for r, f in applying if not r.url]
        if not on_pypi:
            local[key] = _public_url(str(applying[-1][0].url))
            continue
        pins = [(Version(v), v, f) for r, f in on_pypi if (v := _pinned_version(r))]
        if pins:
            _, out[key], pinned_in[key] = max(pins, key=lambda pin: pin[0])
            continue
        out[key] = None
        allowed = SpecifierSet()
        for r, _ in on_pypi:
            allowed &= r.specifier
        if str(allowed):
            ranges[key] = str(allowed)
    return _Declared(out, local, pinned_in, ranges, set(found) - direct, compiled)


# The Python that environment markers are evaluated for when the project names none: newer
# than any a marker compares with, so the declarations for the newest Python count, as the
# newest version does in a uv.lock fork (_pick_forked).
_NEWEST_PYTHON = "3.99"
# The platforms that environment markers are tried on (_applies, _pick_forked), machine
# included, rather than the machine the scan runs on: a project scanned on a Windows laptop,
# a Mac and a Linux CI runner gets the same versions.
_PLATFORMS = tuple(
    {
        "sys_platform": platform,
        "platform_system": system,
        "os_name": os_name,
        "platform_machine": cpu,
    }
    for platform, system, os_name, cpu in (
        ("linux", "Linux", "posix", "x86_64"),
        ("win32", "Windows", "nt", "AMD64"),
        ("darwin", "Darwin", "posix", "arm64"),
    )
)


def _applies(req: Requirement, python: str | None) -> bool:
    """Whether a declaration applies to Python ``python`` (the newest one when None) on
    Linux (x86-64), Windows (x64) or macOS (Apple silicon). Always true for markers it cannot
    evaluate, such as those on extras."""
    marker = req.marker
    if marker is None or "extra" in str(marker):
        return True
    python = python or _NEWEST_PYTHON
    env = {k: str(v) for k, v in default_environment().items()}
    env.update(python_version=python, python_full_version=f"{python}.0")
    try:
        return any(marker.evaluate({**env, **platform}) for platform in _PLATFORMS)
    except _MARKER_ERRORS:
        return True


def _is_root(root: Path, spec: object) -> bool:
    """Whether a Pipfile entry installs the project's own directory (``path = "."``)."""
    path = spec.get("path") if isinstance(spec, dict) else None
    try:
        return isinstance(path, str) and (root / path).resolve() == root
    except (OSError, ValueError):
        return False


def _pipfile_requirement(name: str, spec: object) -> Requirement | None:
    """A Pipfile entry (``requests = "<3"``, ``{version = "==2.0", markers = "..."}``) as a
    requirement; ``"*"`` allows any version."""
    version, markers = spec, None
    if isinstance(spec, dict):
        version, markers = spec.get("version"), spec.get("markers")
    version = str(version or "").strip()
    text = name + ("" if version in ("", "*") else version)
    for candidate in (f"{text}; {markers}" if isinstance(markers, str) else text, text, name):
        try:
            return Requirement(candidate)
        except InvalidRequirement:
            continue
    return None


def _public_url(url: str) -> str:
    """A direct reference's URL without credentials (``https://user:token@host`` -> host)."""
    return re.sub(r"(?<=://)[^/@]*@", "", url)


def _local_sources(data: dict[str, Any]) -> dict[str, str]:
    """Names that [tool.uv.sources] / Poetry tables point at workspaces, paths, git or URLs."""
    out: dict[str, str] = {}
    sources = _table(_table(_table(data.get("tool")).get("uv")).get("sources"))
    for name, spec in sources.items():
        specs = spec if isinstance(spec, list) else [spec]
        for s in specs:
            if isinstance(s, dict):
                hit = next((k for k in ("workspace", "path", "git", "url") if k in s), None)
                if hit:
                    out[canonicalize_name(name)] = hit
    for group in _poetry_groups(data):
        for name, spec in group.items():
            if isinstance(spec, dict):
                hit = next((k for k in ("path", "git", "url") if k in spec), None)
                if hit:
                    out[canonicalize_name(name)] = hit
    return out


def _poetry_groups(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Poetry's dependency tables: main, the old dev-dependencies, and each group's."""
    poetry = _table(_table(data.get("tool")).get("poetry"))
    groups = [_table(poetry.get("dependencies")), _table(poetry.get("dev-dependencies"))]
    groups += [_table(_table(g).get("dependencies")) for g in _table(poetry.get("group")).values()]
    return groups


def _pyproject_requirements(data: dict[str, Any]) -> Iterator[Requirement]:
    project = _table(data.get("project"))
    groups = [project.get("dependencies"), *_table(project.get("optional-dependencies")).values()]
    groups += _table(data.get("dependency-groups")).values()
    specs = [s for group in groups for s in _array(group) if isinstance(s, str)]
    for group in _poetry_groups(data):
        for name, spec in group.items():
            if name.lower() == "python":
                continue
            if isinstance(spec, str):
                version = spec
            elif isinstance(spec, dict):
                version = str(spec.get("version", "") or "")
            else:  # a list of per-marker constraints: treat as unpinned
                version = ""
            specs.append(f"{name}{_poetry_constraint(version)}")
    for spec in specs:
        try:
            yield Requirement(spec)
        except InvalidRequirement:
            continue


def _poetry_constraint(version: str) -> str:
    """A Poetry version constraint as a PEP 440 specifier: ``1.2`` pins, ``>=1,<2`` stays; the
    Poetry-only forms (``^1.2``, ``~1.2``, ``*``) are left out, so they allow any version."""
    version = (version or "").strip()
    if re.fullmatch(r"\d[\w.]*", version):
        return f"=={version}"
    try:
        SpecifierSet(version)
    except InvalidSpecifier:
        return ""
    return version


def _requirement_files(root: Path) -> list[Path]:
    files = sorted(root.glob("requirements*.txt")) + sorted(root.glob("requirements/*.txt"))
    return [f for f in files if f.is_file()]


def parse_requirements(path: Path, _seen: set[Path] | None = None) -> list[Requirement]:
    """Parse a pip requirements file, following ``-r`` includes. Unsupported lines are skipped."""
    seen = _seen if _seen is not None else set()
    path = path.resolve()
    if path in seen or not path.is_file():
        return []
    seen.add(path)
    text = _read_text(path).replace("\\\r\n", " ").replace("\\\n", " ")
    out: list[Requirement] = []
    for raw in text.splitlines():
        line = re.sub(r"(^|\s+)#.*$", "", raw).strip()  # pip's comment rule
        if not line:
            continue
        if line.startswith(("-r ", "--requirement ", "-r\t")):
            out += parse_requirements(path.parent / line.split(None, 1)[1].strip(), seen)
            continue
        if line.startswith("-") or line.startswith((".", "/")):
            continue
        line = re.split(r"\s+--hash", line, maxsplit=1)[0].strip()
        try:
            out.append(Requirement(line))
        except InvalidRequirement:
            continue
    return out


_VIA = re.compile(r"#\s*via\b:?\s*(.*)")
# A ``# via`` entry naming an input of pip-compile rather than a package: ``-r
# requirements.in``, ``app (pyproject.toml)``.
_VIA_INPUT = re.compile(r"^-r\s|^[^-].*\.in$|\((pyproject\.toml|setup\.py|setup\.cfg)\)$")


def _compiled_roles(path: Path) -> dict[str, bool] | None:
    """For the output of pip-compile or ``uv pip compile``, whether each requirement is a
    direct one: its ``# via`` annotation names an input file (``-r requirements.in``,
    ``app (pyproject.toml)``), not only the packages that need it (``# via kombu``). Without
    annotations, the ``.in`` file next to it tells. None for a file written by hand."""
    text = _read_text(path).replace("\\\r\n", " ").replace("\\\n", " ")
    vias: dict[str, list[str]] = {}
    current: str | None = None
    listing = False  # inside a multi-line ``# via`` block
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            via = _VIA.match(line)
            if via and current:
                vias[current] += [v.strip() for v in via.group(1).split(",") if v.strip()]
                listing = True
            elif listing and current and line[1:].strip():
                vias[current].append(line[1:].strip())
            else:
                listing = False
            continue
        listing = False
        req, *comment = re.split(r"\s+#", raw, maxsplit=1)
        name = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", req.strip())
        current = canonicalize_name(name.group(0)) if name else None
        if current is None:
            continue
        vias.setdefault(current, [])
        via = _VIA.match("#" + comment[0]) if comment else None
        if via:  # older pip-compile: ``click==7.1.2  # via flask, -r requirements.in``
            vias[current] += [v.strip() for v in via.group(1).split(",") if v.strip()]
    header = "autogenerated by pip-compile" in text or "uv pip compile" in text
    if any(vias.values()):
        return {name: not v or any(_VIA_INPUT.search(e) for e in v) for name, v in vias.items()}
    if not header:
        return None
    source = path.with_suffix(".in")
    if not source.is_file():
        return {}
    inputs = {canonicalize_name(r.name) for r in parse_requirements(source)}
    return {name: name in inputs for name in vias}


def _pinned_version(req: Requirement) -> str | None:
    specs = list(req.specifier)
    if len(specs) == 1 and specs[0].operator in ("==", "===") and "*" not in specs[0].version:
        try:
            Version(specs[0].version)
        except InvalidVersion:
            return None  # an arbitrary ``===`` string cannot be looked up; treat as unpinned
        return specs[0].version
    return None


# ----------------------------------------------------------- environment
def installed_versions(root: Path) -> dict[str, str]:
    """Read ``*.dist-info/METADATA`` from the project's virtualenv without running Python."""
    for venv in VENV_DIRS:
        base = root / venv
        if not (base / "pyvenv.cfg").is_file():
            continue
        candidates = [*base.glob("lib/python*/site-packages"), base / "Lib" / "site-packages"]
        for site in candidates:
            if site.is_dir():
                return _dist_info_versions(site)
    return {}


def _dist_info_versions(site: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for meta in site.glob("*.dist-info/METADATA"):
        name = version = None
        try:
            with meta.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if line.startswith("Name:"):
                        name = line.split(":", 1)[1].strip()
                    elif line.startswith("Version:"):
                        version = line.split(":", 1)[1].strip()
                    if (name and version) or not line.strip():
                        break
        except OSError:
            continue
        if name and version:
            out[canonicalize_name(name)] = version
    return out


def python_version(root: Path, *, explicit_only: bool = False) -> str | None:
    """The project's Python version from ``.python-version`` (or ``requires-python``)."""
    pv = root / ".python-version"
    if pv.is_file():
        try:
            m = re.match(r"\s*(\d+)\.(\d+)", _read_text(pv))
        except OSError:
            m = None
        if m:
            return f"{m.group(1)}.{m.group(2)}"
    if explicit_only:
        return None
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = _load_toml(pyproject)
        except ProjectError:
            return None
        rp = str(_table(data.get("project")).get("requires-python", ""))
        m = re.search(r">=\s*(\d+)\.(\d+)", rp)
        if m:
            return f"{m.group(1)}.{m.group(2)}"
    return None


# -------------------------------------------------------------- source scan
def iter_source_files(root: Path, *, max_files: int = 5000) -> Iterable[Path]:
    count = 0
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = sorted(d.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir():
                if entry.name not in SKIP_DIRS and not entry.name.startswith("."):
                    stack.append(entry)
            elif entry.suffix == ".py":
                yield entry
                count += 1
                if count >= max_files:
                    return


@dataclass(frozen=True)
class FileUse:
    """What one file of the project's code reaches through its imports (:func:`scan_file`).

    Enough to tell, without inferring types, whether the code plausibly touches a changed API
    of a dependency (:func:`since_cutoff.selection.used_names`).
    """

    # The dotted paths the file imports and the attribute chains it reads on imported names,
    # with their prefixes: ``import numpy as np`` and ``np.linalg.norm(v)`` give ``numpy``,
    # ``numpy.linalg`` and ``numpy.linalg.norm``.
    paths: frozenset[str] = frozenset()
    # Every name read as an attribute: ``send`` in ``client.send(...)``.
    attributes: frozenset[str] = frozenset()
    # Two attribute names in a row: ``messages.create`` in ``client.messages.create(...)``.
    pairs: frozenset[str] = frozenset()
    # The names called, a name imported under another one by its own: ``Client`` in
    # ``Client()``, ``send`` in ``client.send(...)``.
    calls: frozenset[str] = frozenset()
    # ``(callable, keyword)`` of each keyword argument: ``("send", "temperature")``.
    keywords: frozenset[tuple[str, str]] = frozenset()
    # ``(class, attribute)`` of each attribute read on an imported class or on what the file
    # shows is an instance of one: ``app = Starlette(...)`` and ``app.routes``,
    # ``Client().send``, ``def f(c: Client): c.send``, ``self.routes`` in a subclass.
    members: frozenset[tuple[str, str]] = frozenset()
    # The file, relative to the project root, with "/" on every system: ``app/main.py``
    # (:func:`scan_sources`); "" when unknown. Where the code uses a change is file-level for
    # now (selection.uses); the lines and columns are issue #8's.
    file: str = ""
    # Every chain of two or more names read with dots, from its first name (or the first
    # attribute after a call): ``client.beta.messages.create``, ``get().messages.create`` as
    # ``messages.create``. What ``pairs`` cannot tell: whether ``messages.create`` is reached
    # through ``beta``.
    chains: frozenset[str] = frozenset()
    # ``(callable, keyword)`` of each keyword argument whose callable the file shows the path
    # of: an imported name (``fetch(retries=1)`` after ``from toylib import fetch``:
    # ``toylib.fetch``), a name read on an imported module or class (``toylib.fetch``,
    # ``toylib.Client.send``), or a method of what the file shows is an instance of an
    # imported class (``Client().send(...)``, ``c.send(...)`` after ``c = Client()``, and
    # ``self.send(...)`` in a subclass: ``toylib.Client.send``). ``keywords`` has the same
    # arguments by the callable's last name only, which any library's ``create`` shares.
    keyword_paths: frozenset[tuple[str, str]] = frozenset()
    # ``(chain, keyword)`` of each keyword argument passed to a method read at the end of a
    # chain (``chains``): ``("client.messages.create", "temperature")``.
    keyword_chains: frozenset[tuple[str, str]] = frozenset()
    # ``(callable, keyword, module)`` for each keyword argument of ``keyword_paths`` and each
    # top-level module its value reads, directly or through a name the file assigns it to:
    # ``("openai.OpenAI", "http_client", "httpx")`` for ``OpenAI(http_client=httpx.Client())``
    # and for ``c = httpx.Client()`` then ``OpenAI(http_client=c)``. ``OpenAI(timeout=30.0)``
    # reads no module.
    keyword_modules: frozenset[tuple[str, str, str]] = frozenset()
    # ``(callable, position, module)`` the same way for each argument passed by position, from
    # 0, up to a ``*args`` (after it, positions are not known):
    # ``("huggingface_hub.hf_raise_for_status", 0, "httpx")`` for
    # ``hf_raise_for_status(httpx.get(url))``. On ``self`` or ``super()``
    # (``super().__init__(c)``), 0 is the argument after ``self``.
    positional_modules: frozenset[tuple[str, int, str]] = frozenset()
    # ``(exception, module)`` for each exception an ``except`` names and each top-level module
    # its ``try`` body reaches (a name imported from it, or what the file shows is an instance
    # of one of its classes): ``("httpx.HTTPError", "huggingface_hub")`` for ``try:
    # hf_hub_download(...)`` ``except httpx.HTTPError:``.
    handled: frozenset[tuple[str, str]] = frozenset()
    # What :func:`typed` needs to type the file's receivers from the packages' own annotations
    # (:func:`receiver_evidence`): the path each import binds a name to, ``("pd", "pandas")``;
    # each chain of attribute reads and calls from a plain name, with ``()`` where a call is
    # made, ``pd.read_csv().applymap``, ``df.groupby().count``, ``self.model.predict``; what
    # each name (``self.x`` too) is assigned, ``("df", "pd.read_csv()")``, or ``("data", "?")``
    # for a value that is no such chain; what each name is annotated with, ``("df",
    # "pd.DataFrame")``, and what a call of the file's own function gives, ``("load()",
    # "pd.DataFrame")``; the keywords passed at the end of a chain, ``("df.groupby", "axis")``;
    # and the names the file's own classes and functions, and its relative imports (``from
    # .models import Store``), define: a value of theirs is the project's, not a package's.
    bound: frozenset[tuple[str, str]] = frozenset()
    accesses: frozenset[str] = frozenset()
    assigned: frozenset[tuple[str, str]] = frozenset()
    annotated: frozenset[tuple[str, str]] = frozenset()
    chain_keywords: frozenset[tuple[str, str]] = frozenset()
    defined: frozenset[str] = frozenset()
    # ``(base, member)`` for each member a class of the file defines (in itself or an in-file
    # base) over an imported base: ``self.predict()`` in a ``class Mine(ChatX)`` that has its
    # own ``predict`` is not ``ChatX``'s; an unrelated ``def predict`` elsewhere does not count.
    shadowed: frozenset[tuple[str, str]] = frozenset()
    # The imported paths the file uses as types: a class statement's bases, the classes an
    # ``isinstance()`` or ``issubclass()`` checks. A function in their place is an error.
    type_refs: frozenset[str] = frozenset()
    # Set by :func:`typed`. ``reaches``: the paths (with their prefixes, as ``paths``) of the
    # classes the file's typed values derive from in packages the file does not import:
    # ``llm = ChatOpenAI(...)`` from langchain-openai is a langchain-core ``BaseChatModel``, so
    # the file uses langchain-core's API (Project.code_use). ``loose``: of the names typed() was
    # asked to keep, those the file reads or calls on a bare name it shows nothing of (a
    # parameter without an annotation, a loop variable): the name-only tier of selection
    # (NAME_ONLY).
    reaches: frozenset[str] = frozenset()
    loose: frozenset[str] = frozenset()

    def imports(self, import_names: Iterable[str]) -> bool:
        """Whether the file imports a distribution with these import names (``requests``,
        or ``google.genai`` inside a namespace package)."""
        return any(name in self.paths for name in import_names)

    def annotated_chains(self, name: str) -> set[str]:
        """What ``name`` is annotated with (``annotated``), for :func:`typed`."""
        return {chain for n, chain in self.annotated if n == name}

    def reaches_any(self, import_names: Iterable[str]) -> bool:
        """Whether the file holds a value of a class that derives from one of a distribution
        with these import names, without importing it (``reaches``)."""
        return any(name in self.reaches for name in import_names)


class SourceScan(NamedTuple):
    modules: set[str]  # top-level modules the code imports
    files: list[FileUse]  # what each file reaches through its imports


def scan_sources(root: Path) -> SourceScan:
    """Scan the project's own code once.

    The imports tell which dependencies its code uses at all, and what each file reaches
    through them which changes of those dependencies it touches.
    """
    modules: set[str] = set()
    files: list[FileUse] = []
    for path in iter_source_files(root):
        try:
            if path.stat().st_size > 1_000_000:
                continue
            # A SyntaxWarning (DeprecationWarning before 3.12) about the project's own "\W"
            # is not for since-cutoff's user.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                tree = ast.parse(
                    path.read_text(encoding="utf-8", errors="replace"), filename=str(path)
                )
        # Python 3.13 cannot parse some generated code either: 3000 strings joined with "+".
        except (OSError, SyntaxError, ValueError, RecursionError, MemoryError):
            continue
        # ``app/main.py``: relative to the project root, with "/" on every system.
        use = scan_file(tree, path.relative_to(root).as_posix())
        modules.update(p for p in use.paths if "." not in p)
        files.append(use)
    return SourceScan(modules, files)


def scan_file(tree: ast.AST, file: str = "") -> FileUse:
    """What one parsed file reaches through its imports (see :class:`FileUse`); ``file`` is
    its path relative to the project root.

    Imports anywhere in the file count (in functions, under ``if TYPE_CHECKING:``), relative
    imports (the project's own modules) do not. After ``from pkg import *``, a name the file
    does not import otherwise may come from ``pkg``.
    """
    nodes = list(ast.walk(tree))
    bound: dict[str, str] = {}  # local name -> the dotted path it was imported as
    star: list[str] = []
    paths: set[str] = set()

    def reach(path: str) -> None:
        parts = path.split(".")
        paths.update(".".join(parts[:i]) for i in range(1, len(parts) + 1))

    for node in nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                reach(alias.name)
                top = alias.name.split(".")[0]
                bound[alias.asname or top] = alias.name if alias.asname else top
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            reach(node.module)
            for alias in node.names:
                if alias.name == "*":
                    star.append(node.module)
                else:
                    reach(f"{node.module}.{alias.name}")
                    bound[alias.asname or alias.name] = f"{node.module}.{alias.name}"

    def resolve(expr: ast.expr) -> str | None:
        """The imported path an expression names: ``np.linalg`` -> ``numpy.linalg``."""
        chain = _dotted(expr)
        head, _, rest = (chain or "").partition(".")
        if head not in bound:
            return None
        return f"{bound[head]}.{rest}" if rest else bound[head]

    def classes(annotation: ast.expr | None) -> set[str]:
        """The imported names in an annotation (``Starlette | None`` -> Starlette)."""
        found = set()
        for n in ast.walk(annotation) if annotation is not None else ():
            if isinstance(n, (ast.Name, ast.Attribute)) and (path := resolve(n)):
                found.add(path)
        return found

    # What the file shows each variable holds an instance of: ``app = Starlette(...)``,
    # ``with Client() as c``, ``def f(app: Starlette)``, ``self.app: Starlette``.
    instances: dict[str, set[str]] = {}

    def holds(name: str | None, kinds: set[str]) -> None:
        if name and name not in ("self", "cls") and kinds:
            instances.setdefault(name, set()).update(kinds)

    def made(expr: ast.expr) -> set[str]:
        """``{"starlette.applications.Starlette"}`` for ``Starlette(...)``."""
        kind = resolve(expr.func) if isinstance(expr, ast.Call) else None
        return {kind} if kind else set()

    for node in nodes:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                holds(_dotted(target), made(node.value))
        elif isinstance(node, ast.AnnAssign):
            holds(_dotted(node.target), classes(node.annotation))
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    holds(_dotted(item.optional_vars), made(item.context_expr))
        elif isinstance(node, ast.arg):
            holds(node.arg, classes(node.annotation))

    # The top-level modules the value of each name the file assigns reads (``c =
    # httpx.Client()``, ``with httpx.Client() as c``, ``def f(c: httpx.Client)``: httpx),
    # through names assigned from other names too. Scopes are not told apart.
    assigned: list[tuple[str, ast.expr]] = []
    for node in nodes:
        if isinstance(node, ast.Assign):
            assigned += [(d, node.value) for t in node.targets if (d := _dotted(t))]
        elif isinstance(node, ast.AnnAssign) and (d := _dotted(node.target)):
            assigned += [(d, e) for e in (node.annotation, node.value) if e is not None]
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None and (d := _dotted(item.optional_vars)):
                    assigned.append((d, item.context_expr))
        elif isinstance(node, ast.arg) and node.annotation is not None:
            assigned.append((node.arg, node.annotation))
    origins: dict[str, set[str]] = {}

    def modules_read(expr: ast.expr) -> set[str]:
        """The top-level modules an expression reads: ``{"httpx"}`` for ``httpx.Client()``
        and for ``c`` after ``c = httpx.Client()``."""
        found: set[str] = set()
        for n in ast.walk(expr):
            if isinstance(n, ast.Name) and n.id in bound:
                found.add(bound[n.id].split(".")[0])
            if isinstance(n, (ast.Name, ast.Attribute)):
                found |= origins.get(_dotted(n) or "", set())
        return found

    for _ in range(3):  # ``a = httpx.Client()``, ``b = a``, ``c = b``
        grew = False
        for name, expr in assigned:
            new = modules_read(expr) - origins.get(name, set())
            if new:
                origins.setdefault(name, set()).update(new)
                grew = True
        if not grew:
            break

    members: set[tuple[str, str]] = set()
    keyword_paths: set[tuple[str, str]] = set()
    keyword_modules: set[tuple[str, str, str]] = set()
    positional_modules: set[tuple[str, int, str]] = set()
    for node in nodes:
        bases = {p for p in map(resolve, node.bases) if p} if isinstance(node, ast.ClassDef) else ()
        for n in ast.walk(node) if bases else ():
            # ``self.routes`` or ``super().routes`` in a subclass of an imported class.
            if isinstance(n, ast.Attribute) and _on_self(n.value):
                members.update((base, n.attr) for base in bases)
            elif (
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and _on_self(n.func.value)
            ):
                keyword_paths.update(
                    (f"{base}.{n.func.attr}", k.arg) for base in bases for k in n.keywords if k.arg
                )
                # ``super().__init__(http_client=...)`` calls the base class itself.
                method = "" if n.func.attr == "__init__" else f".{n.func.attr}"
                keyword_modules.update(
                    (f"{base}{method}", k.arg, m)
                    for base in bases
                    for k in n.keywords
                    if k.arg
                    for m in modules_read(k.value)
                )
                positional_modules.update(
                    (f"{base}{method}", i, m)
                    for base in bases
                    for i, a in _positional(n.args)
                    for m in modules_read(a)
                )

    def callee_paths(func: ast.expr) -> set[str]:
        """The paths of what a call calls, as far as the file shows them (see keyword_paths)."""
        if isinstance(func, ast.Name):
            if func.id in bound:
                return {bound[func.id]}
            return {f"{module}.{func.id}" for module in star}
        if not isinstance(func, ast.Attribute):
            return set()
        named = resolve(func.value)
        if named:
            return {f"{named}.{func.attr}"}
        kinds = made(func.value) or instances.get(_dotted(func.value) or "", set())
        return {f"{kind}.{func.attr}" for kind in kinds}

    attributes: set[str] = set()
    pairs: set[str] = set()
    chains: set[str] = set()
    calls: set[str] = set()
    keywords: set[tuple[str, str]] = set()
    keyword_chains: set[tuple[str, str]] = set()
    for node in nodes:
        if isinstance(node, ast.Attribute):
            attributes.add(node.attr)
            inner = node.value
            chain = _dotted(node)
            head, _, rest = (chain or "").partition(".")
            if head in bound:  # a path through imported names, not an attribute of a value
                reach(f"{bound[head]}.{rest}")
            elif isinstance(inner, ast.Attribute):
                pairs.add(f"{inner.attr}.{node.attr}")
            elif isinstance(inner, ast.Name):
                pairs.add(f"{inner.id}.{node.attr}")
            names = _chain(node)
            if names:
                chains.add(names)
            named = resolve(inner)  # ``Starlette.routes``, or a module's attribute
            kinds = {named} if named else made(inner) or instances.get(_dotted(inner) or "", set())
            members.update((kind, node.attr) for kind in kinds)
        elif isinstance(node, ast.Name) and star and node.id not in bound:
            for module in star:
                reach(f"{module}.{node.id}")
        elif isinstance(node, ast.Call):
            func = node.func
            callee = None
            if isinstance(func, ast.Attribute):
                callee = func.attr
            elif isinstance(func, ast.Name):
                callee = bound.get(func.id, func.id).rsplit(".", 1)[-1]
            if callee:
                calls.add(callee)
                passed = [k.arg for k in node.keywords if k.arg]
                keywords.update((callee, k) for k in passed)
                targets = callee_paths(func)
                keyword_paths.update((p, k) for p in targets for k in passed)
                keyword_modules.update(
                    (p, k.arg, m)
                    for k in node.keywords
                    if k.arg and targets
                    for m in modules_read(k.value)
                    for p in targets
                )
                positional_modules.update(
                    (p, i, m)
                    for i, a in _positional(node.args)
                    if targets
                    for m in modules_read(a)
                    for p in targets
                )
                names = _chain(func) if isinstance(func, ast.Attribute) else None
                if names:
                    keyword_chains.update((names, k) for k in passed)
    return FileUse(
        frozenset(paths),
        frozenset(attributes),
        frozenset(pairs),
        frozenset(calls),
        frozenset(keywords),
        frozenset(members),
        file,
        frozenset(chains),
        frozenset(keyword_paths),
        frozenset(keyword_chains),
        frozenset(keyword_modules),
        frozenset(positional_modules),
        frozenset(_handled(nodes, resolve, bound, instances)),
        **receiver_evidence(nodes),
    )


def _positional(args: Sequence[ast.expr]) -> Iterator[tuple[int, ast.expr]]:
    """A call's arguments passed by position, with their positions, up to a ``*args``."""
    for i, arg in enumerate(args):
        if isinstance(arg, ast.Starred):
            return
        yield i, arg


def _handled(
    nodes: Sequence[ast.AST],
    resolve: Callable[[ast.expr], str | None],
    bound: Mapping[str, str],
    instances: Mapping[str, set[str]],
) -> set[tuple[str, str]]:
    """FileUse.handled: ``(exception, module)`` for each exception an ``except`` names and
    each top-level module its ``try`` body reaches."""
    out: set[tuple[str, str]] = set()
    tries = (ast.Try, getattr(ast, "TryStar", ast.Try))
    for node in nodes:
        if not isinstance(node, tries):
            continue
        reached: set[str] = set()
        for statement in node.body:
            for n in ast.walk(statement):
                if isinstance(n, ast.Name) and n.id in bound:
                    reached.add(bound[n.id].split(".")[0])
                if isinstance(n, (ast.Name, ast.Attribute)):
                    reached |= {k.split(".")[0] for k in instances.get(_dotted(n) or "", ())}
        for handler in node.handlers:
            kind = handler.type
            named = kind.elts if isinstance(kind, ast.Tuple) else [kind] if kind else []
            for path in filter(None, map(resolve, named)):
                out.update((path, module) for module in reached)
    return out


def _on_self(node: ast.expr) -> bool:
    """``self``, ``cls`` or ``super()``: what a method reads its own class's members on."""
    return (isinstance(node, ast.Name) and node.id in ("self", "cls")) or (
        isinstance(node, ast.Call) and _dotted(node.func) == "super"
    )


def _chain(node: ast.expr) -> str | None:
    """``client.beta.messages.create`` for an attribute chain, from its first name, or from
    the first attribute after anything else (``get().messages.create``: ``messages.create``);
    None for fewer than two names."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts)) if len(parts) >= 2 else None


def _dotted(node: ast.expr) -> str | None:
    """``a.b.c`` for an attribute chain on a plain name, else None (``f().b``)."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    return ".".join([node.id, *reversed(parts)])


def imported_modules(root: Path) -> set[str]:
    """Top-level module names imported anywhere in the project's own code."""
    return scan_sources(root).modules


# ------------------------------------------------------- receiver typing
# What the file shows of its values, for the typing from the packages' own annotations once
# the scan has them (:func:`typed`, called by selection.type_project). scan_file records the
# evidence (:func:`receiver_evidence`); nothing here reads a package.
_CALL = "()"
# A method annotated to return its own class (apidiff.SELF_TYPE): the receiver's class.
_SELF_TYPE = "Self"
# Receivers whose class the file's own class statement gives (the ``members`` of scan_file),
# never a value of their own.
_OWN = ("self", "cls", "super")
# What a call of a builtin gives (``rows = list()``, ``with open(p) as fh``) is no package's.
_BUILTINS = frozenset(dir(builtins))


def receiver_evidence(nodes: Sequence[ast.AST]) -> dict[str, frozenset[Any]]:
    """The fields of :class:`FileUse` that :func:`typed` reads (``bound``, ``accesses``,
    ``assigned``, ``annotated``, ``chain_keywords``, ``defined``, ``shadowed``), and
    ``type_refs``, from a parsed file's nodes."""
    bound: dict[str, str] = {}
    defined: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                bound[alias.asname or top] = alias.name if alias.asname else top
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    continue
                if node.level == 0 and node.module:
                    bound[alias.asname or alias.name] = f"{node.module}.{alias.name}"
                elif node.level:  # ``from .models import Store``: the project's own
                    defined.add(alias.asname or alias.name)
        elif isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            defined.add(node.name)
    classes = {n.name: n for n in nodes if isinstance(n, ast.ClassDef)}
    methods = {
        id(m)
        for c in classes.values()
        for m in c.body
        if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    def lineage(name: str) -> list[ast.ClassDef]:
        """The file's class and its in-file bases, all the way up, nearest first."""
        out: list[ast.ClassDef] = []
        queue = [name]
        while queue and len(out) < 64:
            cls = classes.get(queue.pop(0))
            if cls is None or cls in out:
                continue
            out.append(cls)
            queue += [b.id for b in cls.bases if isinstance(b, ast.Name) and b.id not in bound]
        return out

    def own_members(cls: ast.ClassDef) -> dict[str, ast.AST]:
        """The names a class statement defines in its body: methods, attributes."""
        out: dict[str, ast.AST] = {}
        for m in cls.body:
            if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                out[m.name] = m
            elif isinstance(m, ast.Assign):
                out.update((t.id, m) for t in m.targets if isinstance(t, ast.Name))
            elif isinstance(m, ast.AnnAssign) and isinstance(m.target, ast.Name):
                out[m.target.id] = m
        return out

    def imported_base(expr: ast.expr) -> str | None:
        chain = _dotted(expr)
        head, _, rest = (chain or "").partition(".")
        if head not in bound:
            return None
        return f"{bound[head]}.{rest}" if rest else bound[head]

    type_refs = {p for c in classes.values() for p in map(imported_base, c.bases) if p}
    for node in nodes:
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in ("isinstance", "issubclass")
            and node.func.id not in bound
            and len(node.args) == 2
        ):
            checked = node.args[1]
            kinds = checked.elts if isinstance(checked, ast.Tuple) else [checked]
            type_refs.update(p for p in map(imported_base, kinds) if p)
    shadowed: set[tuple[str, str]] = set()
    # ``self.<m>()`` typed by the return annotation of the ``m`` the reading class reaches in
    # the file (its own, or an in-file base's): only when every class of the file that reads it
    # reaches one, and all of them say the same.
    self_returns: dict[str, dict[str, ast.expr | None]] = {}
    unscoped: set[str] = set()
    for name, cls in classes.items():
        family = lineage(name)
        defs = [own_members(c) for c in family]
        bases = {p for c in family for p in map(imported_base, c.bases) if p}
        shadowed.update((b, m) for b in bases for d in defs for m in d)
        for n in ast.walk(cls):
            if not (
                isinstance(n, ast.Attribute)
                and isinstance(n.value, ast.Name)
                and n.value.id in ("self", "cls")
            ):
                continue
            key = f"{n.value.id}.{n.attr}{_CALL}"
            nearest = next((d[n.attr] for d in defs if n.attr in d), None)
            if not isinstance(nearest, (ast.FunctionDef, ast.AsyncFunctionDef)):
                unscoped.add(key)
                continue
            returns = nearest.returns
            dumped = ast.dump(returns) if returns is not None else ""
            self_returns.setdefault(key, {})[dumped] = returns
    accesses: set[str] = set()
    assigned: set[tuple[str, str]] = set()
    annotated: set[tuple[str, str]] = set()
    chain_keywords: set[tuple[str, str]] = set()

    def assign(target: ast.expr, value: ast.expr | None) -> None:
        if value is None:
            return
        if isinstance(target, (ast.Tuple, ast.List)):
            # ``a, b = x, y``: element by element; ``a, b = f()``: each name holds an element
            # of what ``f()`` gives, a package's only when ``f`` is one of its names.
            elements = value.elts if isinstance(value, (ast.Tuple, ast.List)) else None
            starred = any(isinstance(e, ast.Starred) for e in (*target.elts, *(elements or ())))
            if elements is not None and len(elements) == len(target.elts) and not starred:
                for t, v in zip(target.elts, elements, strict=True):
                    assign(t, v)
                return
            chain = _access(value)
            if chain is None or _tokens(chain)[0] not in bound:
                for t in target.elts:
                    assign(t.value if isinstance(t, ast.Starred) else t, ast.Constant(None))
            return
        name = _dotted(target)
        if name is None:
            return
        assigned.add((name, _access(value) or "?"))

    def annotate(name: str | None, annotation: ast.expr | None) -> None:
        for chain in _annotation_chains(annotation) if name else ():
            annotated.add((name or "", chain))

    for node in nodes:
        if isinstance(node, ast.Attribute):
            chain = _access(node)
            if chain is not None:
                accesses.add(chain)
        elif isinstance(node, ast.Call):
            chain = _access(node.func)
            if chain is not None:
                chain_keywords.update((chain, k.arg) for k in node.keywords if k.arg)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                assign(target, node.value)
        elif isinstance(node, ast.AnnAssign):
            assign(node.target, node.value)
            annotate(_dotted(node.target), node.annotation)
        elif isinstance(node, ast.NamedExpr):  # ``(m := pattern.match(s))``
            assign(node.target, node.value)
        elif isinstance(node, ast.ExceptHandler) and node.name and node.type is not None:
            # ``except ValueError as err``: an instance of what it names.
            kinds = node.type.elts if isinstance(node.type, ast.Tuple) else [node.type]
            for kind in kinds:
                chain = _access(kind)
                assigned.add((node.name, f"{chain}{_CALL}" if chain else "?"))
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    assign(item.optional_vars, item.context_expr)
        elif isinstance(node, ast.arg):
            annotate(node.arg, node.annotation)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and id(node) not in methods:
            # A function's return, called by its own name: not a method's of the same name.
            annotate(f"{node.name}{_CALL}", node.returns)
    # A method's return, read through ``self``: ``self.make()`` after ``def make(self) ->
    # pd.DataFrame`` in the class (or an in-file base), never another class's ``make``.
    for key, said in self_returns.items():
        if key not in unscoped and len(said) == 1:
            annotate(key, next(iter(said.values())))
    return {
        "bound": frozenset(bound.items()),
        "accesses": frozenset(accesses),
        "assigned": frozenset(assigned),
        "annotated": frozenset(annotated),
        "chain_keywords": frozenset(chain_keywords),
        "defined": frozenset(defined),
        "shadowed": frozenset(shadowed),
        "type_refs": frozenset(type_refs),
    }


def _access(node: ast.expr) -> str | None:
    """``df.groupby().count`` for a chain of attribute reads and calls on a plain name, with
    ``()`` where a call is made (its arguments left out); None for anything else (a subscript,
    a literal, an operator)."""
    parts: list[str] = []
    while True:
        if isinstance(node, ast.Attribute):
            parts.append(f".{node.attr}")
            node = node.value
        elif isinstance(node, ast.Call):
            parts.append(_CALL)
            node = node.func
        elif isinstance(node, ast.Name):
            parts.append(node.id)
            break
        else:
            return None
    return "".join(reversed(parts))


# Subscripted forms whose arguments are the type itself: ``Optional[X]``, ``Union[X, None]``,
# ``Annotated[X, ...]`` (its first argument). Any other (``list[X]``) is a container of it.
_WRAPPING_FORMS = {"Optional", "Union", "Annotated"}


def _annotation_chains(annotation: ast.expr | None) -> list[str]:
    """The names an annotation says a value is an instance of: ``X``, each side of ``X | Y``,
    the arguments of ``Optional``, ``Union`` and ``Annotated`` (its first), a string
    annotation parsed; not the elements of a container (``list[X]``)."""
    if annotation is None:
        return []
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        try:
            annotation = ast.parse(annotation.value.strip(), mode="eval").body
        except (SyntaxError, ValueError):
            return []
    if isinstance(annotation, (ast.Name, ast.Attribute)):
        chain = _access(annotation)
        return [chain] if chain else []
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        return _annotation_chains(annotation.left) + _annotation_chains(annotation.right)
    if isinstance(annotation, ast.Subscript):
        form = _dotted(annotation.value) or ""
        if form.rsplit(".", 1)[-1] not in _WRAPPING_FORMS:
            return []
        inner = annotation.slice
        elements = list(inner.elts) if isinstance(inner, ast.Tuple) else [inner]
        if form.endswith("Annotated"):
            elements = elements[:1]
        return [c for e in elements for c in _annotation_chains(e)]
    return []


class ApiTypes:
    """What the pinned releases' own annotations say the project's values are, from the
    ``receiver_map`` of each (apidiff.receiver_map), to type the receivers of the project's
    code (:func:`typed`). Every lookup is by canonical class path; :meth:`canon` turns a public
    path into one, through the re-exports of every package given (``langchain.chat_models
    .BaseChatModel`` -> ``langchain_core.language_models.BaseChatModel`` ->
    ``langchain_core.language_models.chat_models.BaseChatModel``)."""

    def __init__(self, maps: Iterable[Mapping[str, Mapping[str, Any]]] = ()) -> None:
        self.classes: dict[str, str] = {}
        self.bases: dict[str, list[str]] = {}
        self.returns: dict[str, str] = {}
        self.attrs: dict[str, str] = {}
        # The names each class declares itself (not inherited), by canonical path.
        self.members: dict[str, set[str]] = {}
        for m in maps:
            self.classes.update({str(k): str(v) for k, v in dict(m.get("classes") or {}).items()})
            self.bases.update(
                {str(k): [str(b) for b in v] for k, v in dict(m.get("bases") or {}).items()}
            )
            self.returns.update({str(k): str(v) for k, v in dict(m.get("returns") or {}).items()})
            self.attrs.update({str(k): str(v) for k, v in dict(m.get("attrs") or {}).items()})
            for k, v in dict(m.get("members") or {}).items():
                self.members.setdefault(str(k), set()).update(str(n) for n in v)
        self.public: dict[str, list[str]] = {}
        for public, canonical in self.classes.items():
            self.public.setdefault(self.canon(canonical), []).append(public)
        self._known = set(self.public) | set(self.bases)
        # The top-level modules the maps cover (``pandas``): what a value from another module
        # (``re.match(...)``) is not.
        self.modules = {p.split(".")[0] for p in (*self.classes, *self.returns, *self.attrs)}

    def __bool__(self) -> bool:
        return bool(self.classes or self.returns)

    def canon(self, path: str) -> str:
        """The canonical path of a class, from any public path of it."""
        for _ in range(8):
            target = self.classes.get(path)
            if target is None or target == path:
                break
            path = target
        return path

    def is_class(self, path: str) -> bool:
        return self.canon(path) in self._known

    def ancestors(self, canonical: str) -> list[str]:
        """The base classes of a class, all the way up, canonical paths, nearest first."""
        out: list[str] = []
        queue = [canonical]
        while queue and len(out) < 64:
            cls = queue.pop(0)
            for base in self.bases.get(cls, ()):
                base = self.canon(base)
                if base != canonical and base not in out:
                    out.append(base)
                    queue.append(base)
        return out

    def paths_of(self, canonical: str) -> list[str]:
        """The class's canonical path and every public path of it, in every package."""
        return list(dict.fromkeys([canonical, *self.public.get(canonical, ())]))

    def defines(self, canonical: str, name: str) -> bool:
        """Whether the class declares the member itself in the pinned release (``members``):
        a subclass's own ``sum`` is not its base's, whatever the base's became."""
        return name in self.members.get(canonical, ())

    def member(self, canonical: str, name: str, *, call: bool) -> set[str]:
        """What a member of a class (or of a base) holds: the classes a call of the method
        returns (``call``), or the class of the property or attribute read without one."""
        table = self.returns if call else self.attrs
        for cls in (canonical, *self.ancestors(canonical)):
            found = table.get(f"{cls}.{name}")
            if found is not None:
                return self._classes(found, canonical)
        return set()

    def returned(self, path: str) -> set[str]:
        """The classes a call of a function under this public path returns."""
        found = self.returns.get(path)
        return self._classes(found, None) if found is not None else set()

    def _classes(self, found: str, receiver: str | None) -> set[str]:
        if found == _SELF_TYPE:
            return {receiver} if receiver else set()
        return {self.canon(c) for c in found.split("|") if c}


def typed(
    f: FileUse,
    api: ApiTypes,
    keep: Collection[str] = (),
    owners: Mapping[str, Collection[str]] | None = None,
) -> FileUse:
    """``f`` with the members, keyword paths and reached packages that typing its receivers
    from the packages' own annotations adds, and ``loose`` set to the names of ``keep`` it
    reads or calls on values it could not type.

    A value is typed from the file's own evidence (``assigned``, ``annotated``) and ``api``: a
    name assigned from a call of a package function or class, or from a method of a typed
    value (``df = pd.read_csv(p)``, ``sub = df.head()``), gets the class the annotation names
    (the class itself for a class); a chain types each step the same way (``df.groupby(k)
    .count()`` is ``DataFrameGroupBy.count``); an annotated parameter or variable, and the
    file's own functions' returns, type what they name. A class is read with its bases, in
    every package ``api`` knows (``ChatOpenAI`` is a ``BaseChatModel`` of langchain-core), and
    under each of its public paths, so that the members match the paths a diff records
    (``pandas.DataFrame.applymap``). The scan's own ``members`` (``self.x`` in a subclass, an
    imported class's instance) are widened to their bases the same way. A member is read up
    to the nearest class that defines it in the pinned release (``api.defines``), or that
    ``owners`` (member name -> the classes a diff says changed it, canonical paths) names:
    ``df.sum()`` on a ``DataFrame`` is ``DataFrame.sum``, which pandas 3 still defines, not
    the ``NDFrame.sum`` whose parameters became keyword-only; ``llm.predict`` on a
    ``ChatOpenAI`` is ``BaseChatModel.predict``, not also its base ``BaseLanguageModel``'s,
    which the diff reports on its own. Nothing is run.
    """
    bound = dict(f.bound)
    tops = {p.split(".")[0] for p in bound.values()}
    # What each name holds; ``load()`` and ``self.make()``, with their ``()``, what a call of
    # the file's own function gives.
    types: dict[str, set[str]] = {}

    def resolve(chain: str) -> set[str]:
        """The classes a chain of the file holds (canonical paths), as far as it can be told."""
        tokens = _tokens(chain)
        if not tokens:
            return set()
        kinds: set[str] = set()
        path: str | None = None  # an import path, followed while it is one
        pending: set[tuple[str, str]] = set()  # methods read, not yet called
        known = False  # whether some prefix of the chain is a typed name or an import
        prefix = ""
        for i, token in enumerate(tokens):
            prefix += token
            if prefix in types:  # a name, or ``self.model`` assigned or annotated as a whole
                kinds, path, pending, known = set(types[prefix]), None, set(), True
                continue
            if i == 0:
                if token in bound:
                    path, known = bound[token], True
                continue
            if not known:
                continue  # ``self``, ``cfg``: a later prefix may still be typed
            if token == _CALL:
                if path is not None:
                    if api.is_class(path):
                        kinds = {api.canon(path)}
                    else:
                        kinds = api.returned(path)
                        owner, _, name = path.rpartition(".")
                        if not kinds and api.is_class(owner):  # ``pd.DataFrame.from_dict()``
                            kinds = api.member(api.canon(owner), name, call=True)
                    path = None
                elif pending:
                    kinds = {k for cls, name in pending for k in api.member(cls, name, call=True)}
                    pending = set()
                else:
                    return set()  # calling an instance, or a value nothing is known of
            else:
                name = token[1:]
                if path is not None:
                    if api.is_class(path):
                        # A class's attribute or method, read on the class: ``pd.DataFrame.T``.
                        cls = api.canon(path)
                        kinds, pending = api.member(cls, name, call=False), {(cls, name)}
                        path = None
                    else:
                        path = f"{path}.{name}"
                elif kinds:
                    read = {k for cls in kinds for k in api.member(cls, name, call=False)}
                    pending = set() if read else {(cls, name) for cls in kinds}
                    kinds = read
                else:
                    return set()
            if path is None and not kinds and not pending:
                return set()
        if not known:
            return set()
        if path is not None:
            return {api.canon(path)} if api.is_class(path) else set()
        return kinds

    for name, chain in f.annotated:
        found = resolve(chain)
        if found:
            types.setdefault(name, set()).update(found)
    for _ in range(4):  # ``a = pd.read_csv(p)``, ``b = a.head()``, ``c = b``
        grew = False
        for name, chain in f.assigned:
            new = resolve(chain) - types.get(name, set()) if chain != "?" else set()
            if new:
                types.setdefault(name, set()).update(new)
                grew = True
        if not grew:
            break

    members = set(f.members)
    keyword_paths = set(f.keyword_paths)
    reached: set[str] = set()

    def widen(classes: Iterable[str], attr: str) -> list[str]:
        """Every path of these classes and of their bases, in every package, up to the
        nearest class that defines ``attr`` itself or that ``owners`` says changed it."""
        out: list[str] = []
        changed = (owners or {}).get(attr, ())
        for cls in classes:
            chain = [cls, *api.ancestors(cls)]
            nearest = next(
                (i for i, c in enumerate(chain) if c in changed or api.defines(c, attr)), None
            )
            for c in chain if nearest is None else chain[: nearest + 1]:
                for p in api.paths_of(c):
                    out.append(p)
                    reached.update(_prefixes(p))
        return list(dict.fromkeys(out))

    # The scan's own members, read on a class it names: its bases too, but not for a member a
    # subclass of the file defines itself (``self.make()`` in one that has its own ``make``).
    for cls_path, attr in list(members):
        if api.is_class(cls_path) and (cls_path, attr) not in f.shadowed:
            members.update((p, attr) for p in widen([api.canon(cls_path)], attr))
    for path, keyword in list(keyword_paths):
        owner, _, name = path.rpartition(".")
        if api.is_class(owner) and (owner, name) not in f.shadowed:
            paths = widen([api.canon(owner)], name)
            keyword_paths.update((f"{p}.{name}", keyword) for p in paths)
    # Chains: ``df.applymap``, ``pd.read_csv().applymap``, ``df.groupby().count``.
    untyped: set[str] = set()
    # A name assigned something that is no chain (a literal, a comprehension), a chain from a
    # package no map covers (``m = re.match(...)``), from the file's own classes, functions and
    # relative imports (``loc = Local()``, ``df = self.make()``, ``from .models import Store``)
    # or from a builtin (``rows = list()``), or annotated with a builtin or such a package's
    # class (``data: dict``), is not a package's value left untyped.
    foreign: set[str] = set()
    for name, chain in (*f.assigned, *f.annotated):
        tokens = _tokens(chain)
        root = tokens[0]
        covered = root in bound and bound[root].split(".")[0] in api.modules
        own = root not in bound and (
            root in f.defined
            or root in _BUILTINS
            or (root in _OWN and len(tokens) > 1 and tokens[1][1:] in f.defined)
        )
        if (
            chain == "?"
            or own
            or (not covered and (root in bound or chain in f.annotated_chains(name)))
        ):
            foreign.add(name)
    # And through what the file assigns from such a value: ``alias = loc``, ``store =
    # self.store`` after ``self.store = Store()``, ``rows = data.items()``.
    for _ in range(4):
        grew = False
        for name, chain in f.assigned:
            if name in foreign or chain == "?":
                continue
            prefix = ""
            for token in _tokens(chain):
                prefix += token
                if prefix in foreign:
                    foreign.add(name)
                    grew = True
                    break
        if not grew:
            break
    for chain in f.accesses:
        receiver, _, attr = chain.rpartition(".")
        kinds = resolve(receiver)
        if kinds:
            members.update((p, attr) for p in widen(kinds, attr))
        elif (
            receiver
            and "." not in receiver
            and not receiver.endswith(_CALL)
            and receiver not in bound
            and receiver not in types
            and receiver not in foreign
            and receiver not in _OWN
        ):
            untyped.add(attr)  # read on a bare name the file shows nothing of
    for chain, keyword in f.chain_keywords:
        receiver, _, name = chain.rpartition(".")
        kinds = resolve(receiver) if receiver else set()
        if kinds:
            keyword_paths.update((f"{p}.{name}", keyword) for p in widen(kinds, name))
    reaches = frozenset(p for p in reached if p.split(".")[0] not in tops)
    return replace(
        f,
        members=frozenset(members),
        keyword_paths=frozenset(keyword_paths),
        reaches=reaches,
        loose=frozenset(untyped & set(keep)),
    )


def _tokens(chain: str) -> list[str]:
    """``["df", ".groupby", "()", ".count"]`` for ``df.groupby().count``."""
    if not chain:
        return []
    return [t for t in re.split(r"(\(\)|\.[^.()]+)", chain) if t]


def _prefixes(path: str) -> list[str]:
    parts = path.split(".")
    return [".".join(parts[:i]) for i in range(1, len(parts) + 1)]
