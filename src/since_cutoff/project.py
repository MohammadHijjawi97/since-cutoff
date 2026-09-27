"""Discover a Python project's dependencies and the exact versions it uses.

Version sources, most authoritative first: a lockfile (uv.lock, poetry.lock, pdm.lock,
pylock.toml, Pipfile.lock), the project's virtual environment, pinned requirement files, and
finally "latest release on PyPI" for anything that is declared but not pinned.

Dependencies that do not come from PyPI (git, local paths, workspace members, private indexes)
are recorded but never looked up on PyPI by name.
"""

from __future__ import annotations

import ast
import codecs
import json
import re
import sys
import warnings
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
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

    def direct(self) -> list[Dependency]:
        return [d for d in self.dependencies if d.direct]

    def imports(self, import_names: Iterable[str]) -> bool:
        """Whether the project's code imports any of a distribution's import names.

        A name inside a namespace package (``google.cloud.storage``) counts only when that
        package or a module in it is imported: ``from google import genai`` imports
        google-genai, not google-cloud-storage.
        """
        return bool(self.code_use(import_names))

    def code_use(self, import_names: Iterable[str]) -> tuple[FileUse, ...]:
        """The files of the project's code that import any of a distribution's import names
        (as :meth:`imports` counts them)."""
        names = tuple(import_names)
        return tuple(f for f in self.files if f.imports(names))


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
        deps.append(Dependency(key, version, direct, source or "unpinned", non_pypi, specifier))

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
    build = data.get("build-system") or {}
    backend = str(build.get("build-backend") or "")
    requires = " ".join(str(r) for r in build.get("requires") or []).lower()
    return tool in (data.get("tool") or {}) or backend.startswith(tool) or tool in requires


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
    entries = data.get("packages") if path.name.startswith("pylock") else data.get("package")
    for p in entries or []:
        if not isinstance(p, dict) or not p.get("name") or not p.get("version"):
            continue
        name = canonicalize_name(p["name"])
        lock.versions[name] = str(p["version"])
        reason = _non_pypi_reason(p)
        if reason:
            lock.non_pypi[name] = reason
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
    for pkg in data.get("package", []):
        name = canonicalize_name(pkg.get("name", ""))
        source = pkg.get("source") or {}
        # Editable/virtual sources are the project itself (or workspace members): their
        # dependencies are the project's direct dependencies.
        if "editable" in source or "virtual" in source:
            lock.members.add(name)
            for dep in pkg.get("dependencies", []):
                lock.direct.add(canonicalize_name(dep["name"]))
            for group in (pkg.get("optional-dependencies") or {}).values():
                lock.direct.update(canonicalize_name(d["name"]) for d in group)
            for group in (pkg.get("dev-dependencies") or {}).values():
                lock.direct.update(canonicalize_name(d["name"]) for d in group)
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
        candidates.setdefault(name, []).append(
            (parsed, version, list(pkg.get("resolution-markers") or []))
        )
    for locked_name, entries in candidates.items():
        lock.versions[locked_name] = _pick_forked(entries, python)
    lock.direct -= lock.members
    return lock


def _pick_forked(entries: list[tuple[Version, str, list[str]]], python: str | None) -> str:
    """uv locks one version per environment fork; pick the one for the project's Python."""
    if len(entries) == 1 or python is None:
        return max(entries)[1]
    env = {"python_version": python, "python_full_version": f"{python}.0"}
    matching = []
    for parsed, raw, markers in entries:
        try:
            ok = not markers or any(Marker(m).evaluate(env) for m in markers)
        except InvalidMarker:
            ok = True
        if ok:
            matching.append((parsed, raw, markers))
    return max(matching or entries)[1]


def _parse_pipfile_lock(path: Path) -> _Lock:
    try:
        data = json.loads(_read_text(path))
    except (OSError, ValueError) as exc:
        raise ProjectError(f"could not parse {path.name}: {exc}") from exc
    lock = _Lock()
    for section in ("default", "develop"):
        for name, spec in (data.get(section) or {}).items():
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
        project_name = canonicalize_name(str(data.get("project", {}).get("name", "")))
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
                for name, spec in data.get(section, {}).items():
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
# The platforms a declaration's environment markers are tried on (see _applies).
_PLATFORMS = (
    {"sys_platform": "linux", "platform_system": "Linux", "os_name": "posix"},
    {"sys_platform": "win32", "platform_system": "Windows", "os_name": "nt"},
    {"sys_platform": "darwin", "platform_system": "Darwin", "os_name": "posix"},
)


def _applies(req: Requirement, python: str | None) -> bool:
    """Whether a declaration applies to Python ``python`` (the newest one when None) on
    Linux, Windows or macOS. Always true for markers it cannot evaluate, such as those on
    extras."""
    marker = req.marker
    if marker is None or "extra" in str(marker):
        return True
    python = python or _NEWEST_PYTHON
    env = {k: str(v) for k, v in default_environment().items()}
    env.update(python_version=python, python_full_version=f"{python}.0")
    try:
        return any(marker.evaluate({**env, **platform}) for platform in _PLATFORMS)
    except (InvalidMarker, UndefinedComparison, UndefinedEnvironmentName):
        return True


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
    sources = data.get("tool", {}).get("uv", {}).get("sources") or {}
    for name, spec in sources.items():
        specs = spec if isinstance(spec, list) else [spec]
        for s in specs:
            if isinstance(s, dict):
                hit = next((k for k in ("workspace", "path", "git", "url") if k in s), None)
                if hit:
                    out[canonicalize_name(name)] = hit
    poetry = data.get("tool", {}).get("poetry", {})
    groups = [poetry.get("dependencies", {}), poetry.get("dev-dependencies", {})]
    groups += [g.get("dependencies", {}) for g in (poetry.get("group") or {}).values()]
    for group in groups:
        for name, spec in group.items():
            if isinstance(spec, dict):
                hit = next((k for k in ("path", "git", "url") if k in spec), None)
                if hit:
                    out[canonicalize_name(name)] = hit
    return out


def _pyproject_requirements(data: dict[str, Any]) -> Iterator[Requirement]:
    project = data.get("project", {})
    specs: list[str] = list(project.get("dependencies", []))
    for group in (project.get("optional-dependencies") or {}).values():
        specs.extend(group)
    for group in (data.get("dependency-groups") or {}).values():
        specs.extend(s for s in group if isinstance(s, str))
    poetry = data.get("tool", {}).get("poetry", {})
    poetry_groups = [poetry.get("dependencies", {}), poetry.get("dev-dependencies", {})]
    poetry_groups += [g.get("dependencies", {}) for g in (poetry.get("group") or {}).values()]
    for group in poetry_groups:
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
        rp = str(data.get("project", {}).get("requires-python", ""))
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

    def imports(self, import_names: Iterable[str]) -> bool:
        """Whether the file imports a distribution with these import names (``requests``,
        or ``google.genai`` inside a namespace package)."""
        return any(name in self.paths for name in import_names)


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
        except (OSError, SyntaxError, ValueError):
            continue
        use = scan_file(tree)
        modules.update(p for p in use.paths if "." not in p)
        files.append(use)
    return SourceScan(modules, files)


def scan_file(tree: ast.AST) -> FileUse:
    """What one parsed file reaches through its imports (see :class:`FileUse`).

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

    members: set[tuple[str, str]] = set()
    for node in nodes:
        bases = {p for p in map(resolve, node.bases) if p} if isinstance(node, ast.ClassDef) else ()
        for n in ast.walk(node) if bases else ():
            # ``self.routes`` or ``super().routes`` in a subclass of an imported class.
            if isinstance(n, ast.Attribute) and (
                (isinstance(n.value, ast.Name) and n.value.id in ("self", "cls"))
                or (isinstance(n.value, ast.Call) and _dotted(n.value.func) == "super")
            ):
                members.update((base, n.attr) for base in bases)

    attributes: set[str] = set()
    pairs: set[str] = set()
    calls: set[str] = set()
    keywords: set[tuple[str, str]] = set()
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
                keywords.update((callee, k.arg) for k in node.keywords if k.arg)
    return FileUse(
        frozenset(paths),
        frozenset(attributes),
        frozenset(pairs),
        frozenset(calls),
        frozenset(keywords),
        frozenset(members),
    )


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
