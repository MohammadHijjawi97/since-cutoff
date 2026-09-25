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
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from packaging.markers import InvalidMarker, Marker
from packaging.requirements import InvalidRequirement, Requirement
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


@dataclass
class Dependency:
    name: str
    version: str | None
    direct: bool
    source: str
    non_pypi: str | None = None  # why it must not be looked up on PyPI (git, path, index, ...)

    @property
    def key(self) -> str:
        return canonicalize_name(self.name)


@dataclass
class Project:
    root: Path
    dependencies: list[Dependency]
    version_source: str
    python_version: str | None = None
    imported_modules: set[str] = field(default_factory=set)
    identifiers: set[str] = field(default_factory=set)

    def direct(self) -> list[Dependency]:
        return [d for d in self.dependencies if d.direct]


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
    declared, local = _declared_dependencies(root)
    workspace = {name for name, reason in local.items() if reason == "workspace"}
    local = {name: reason for name, reason in local.items() if name not in workspace}
    lock = _Lock()
    version_source = ""
    lockfile = next((root / n for n in LOCKFILES if (root / n).is_file()), None)
    if lockfile is None:
        pylocks = sorted(root.glob("pylock.*.toml"))
        lockfile = pylocks[0] if pylocks else None
    if lockfile is not None:
        lock = parse_lock(lockfile, python=pv)
        version_source = lockfile.name

    installed = installed_versions(root)
    if not lock.versions and installed:
        version_source = next(d for d in VENV_DIRS if (root / d).is_dir())

    lock.members |= workspace
    direct_names = (set(declared) | lock.direct) - lock.members
    if not direct_names and lock.versions:
        direct_names = set(lock.versions)
    if not direct_names and installed:
        direct_names = set(installed)
    if not direct_names and not local:
        raise ProjectError(
            f"no dependencies found in {root}. Expected pyproject.toml, requirements*.txt, "
            "a lockfile (uv.lock, poetry.lock, ...) or a .venv."
        )

    deps: list[Dependency] = []
    for key in sorted((set(lock.versions) | direct_names | set(local)) - lock.members):
        version, source = None, ""
        if key in lock.versions:
            version, source = lock.versions[key], version_source
        elif key in installed:
            version, source = installed[key], "installed"
        elif declared.get(key):
            version, source = declared[key], "pinned"
        non_pypi = lock.non_pypi.get(key) or local.get(key)
        deps.append(
            Dependency(
                key, version, key in direct_names or key in local, source or "unpinned", non_pypi
            )
        )

    modules, identifiers = scan_sources(root)
    return Project(
        root=root,
        dependencies=deps,
        version_source=version_source or ("requirements" if declared else "declared"),
        python_version=python_version(root),
        imported_modules=modules,
        identifiers=identifiers,
    )


# ---------------------------------------------------------------- lockfiles
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
        for key in ("git", "path", "directory", "url", "editable", "virtual"):
            if key in source:
                return key
        registry = str(source.get("registry") or source.get("url") or "")
        kind = str(source.get("type") or "")
        if kind and kind not in ("pypi", ""):
            return kind if kind != "legacy" else "private index"
        if registry and not any(h in registry for h in PYPI_HOSTS):
            return "private index"
    for key in ("git", "path", "vcs", "directory", "archive", "url"):  # pdm / pylock
        if key in entry:
            return key
    index = str(entry.get("index") or "")
    if index and not any(h in index for h in PYPI_HOSTS):
        return "private index"
    return None


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
def _declared_dependencies(root: Path) -> tuple[dict[str, str | None], dict[str, str]]:
    """Direct dependencies: ``({name: pinned_version_or_None}, {name: non_pypi_reason})``."""
    out: dict[str, str | None] = {}
    local: dict[str, str] = {}

    def add(req: Requirement) -> None:
        key = canonicalize_name(req.name)
        if req.url:
            local[key] = "direct URL"
            return
        pinned = _pinned_version(req)
        if pinned or key not in out:
            out[key] = pinned

    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        data = _load_toml(pyproject)
        project_name = canonicalize_name(str(data.get("project", {}).get("name", "")))
        for name, reason in _local_sources(data).items():
            local[name] = reason
        for req in _pyproject_requirements(data):
            if canonicalize_name(req.name) != project_name:
                add(req)

    pipfile = root / "Pipfile"
    if pipfile.is_file():
        try:
            data = _load_toml(pipfile)
            for section in ("packages", "dev-packages"):
                for name, spec in data.get(section, {}).items():
                    if isinstance(spec, dict) and spec.keys() & {"git", "path", "file", "editable"}:
                        local[canonicalize_name(name)] = "local or git"
                    else:
                        out.setdefault(canonicalize_name(name), None)
        except ProjectError:
            pass

    for req_file in _requirement_files(root):
        for req in parse_requirements(req_file):
            add(req)
    for name in local:
        out.pop(name, None)
    return out, local


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
            pin = f"=={version}" if re.fullmatch(r"\d[\w.]*", version or "") else ""
            specs.append(f"{name}{pin}")
    for spec in specs:
        try:
            yield Requirement(spec)
        except InvalidRequirement:
            continue


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


def scan_sources(root: Path) -> tuple[set[str], set[str]]:
    """Scan the project's own code once.

    Returns ``(imported_top_level_modules, identifiers)`` where identifiers are the attribute
    names, keyword-argument names and imported names the code uses. They are used to rank API
    changes by how likely they are to matter for this project.
    """
    modules: set[str] = set()
    identifiers: set[str] = set()
    for path in iter_source_files(root):
        try:
            if path.stat().st_size > 1_000_000:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError, ValueError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                modules.add(node.module.split(".")[0])
                identifiers.update(a.name for a in node.names)
            elif isinstance(node, ast.Attribute):
                identifiers.add(node.attr)
            elif isinstance(node, ast.keyword) and node.arg:
                identifiers.add(node.arg)
    return modules, identifiers


def imported_modules(root: Path) -> set[str]:
    """Top-level module names imported anywhere in the project's own code."""
    return scan_sources(root)[0]
