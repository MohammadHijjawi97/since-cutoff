"""The since-cutoff pipeline: scan -> select -> tasks -> probe -> notes -> test.

1. **Scan.** For each dependency, find the version that was current at the model's training
   cutoff and statically diff its public API against the version the project uses.
2. **Select.** Rank the breaking changes and pick a probe budget.
3. **Tasks.** A task-writer model turns each change into short, realistic coding tasks that
   never name the changed identifier or its replacement. One task probes; the others are held
   out.
4. **Probe.** The model under test answers the probe task with no tools and no docs. Its code
   is type-checked against *both* versions (each with its own runtime dependencies). An answer
   is **stale** when a statement that is valid for the cutoff version is invalid for yours and
   the error involves an API that changed; **wrong** when the package API errors are not
   explained by a change; **untouched** when the answer never exercises the changed API.
5. **Notes.** For each failure the task writer writes a short AGENTS.md note with an example; it
   is kept only when the example type-checks against the pinned version (``[type-checked]``),
   and the note from the API diff (:func:`notes.diff_note`) takes its place otherwise.
6. **Test.** Held-out tasks are answered again with and without the notes and compared as
   pairs, and a sample of previously-correct APIs checks that the notes do not hurt. A change
   counts as fixed when more than half of its counted pairs are wrong without the notes and
   right with them (:meth:`RunResult.pairing`, :attr:`ChangePairs.outcome`). With
   ``--compare``, every held-out task is also answered with each baseline block
   (:mod:`since_cutoff.baselines`), and the blocks are compared on the same pairs: those every
   block could count (``RunResult.pairing(..., common=True)``).
"""

from __future__ import annotations

import ast
import logging
import os
import re
import sys
from collections.abc import Callable, Iterable, Iterator, Mapping
from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Any, TypeVar, cast

from packaging.markers import InvalidMarker, default_environment
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from since_cutoff import __version__, hosts, prompts
from since_cutoff.apidiff import (
    DEPENDENCY_SWITCHED,
    DIFF_SCHEMA,
    MOVED,
    PARAM_KEYWORD_ONLY,
    PARAM_POSITIONAL_ONLY,
    PARAM_REMOVED,
    PARAM_REQUIRED,
    TIER_INTERNAL,
    APIChange,
    dependency_module,
    diff_sources_with_unread,
    griffe_version,
    load_api,
    requirements,
    unread_text,
)
from since_cutoff.apidiff import (
    DEPRECATED as CHANGE_DEPRECATED,
)
from since_cutoff.baselines import (
    ARM_SIGNATURES,
    ARM_TEMPLATE,
    ARM_VERIFIED,
    signature_notes,
    template_notes,
)
from since_cutoff.cache import DiskCache, stable_hash
from since_cutoff.checker import Checker, CheckResult, Diagnostic, extract_code
from since_cutoff.errors import CheckerError, NoCodeError, PackageIndexError, ProviderError
from since_cutoff.models import (
    DEFAULT_CUTOFF_MARGIN,
    PROVIDER_ALIASES,
    ModelInfo,
    ModelRegistry,
    compare_date,
)
from since_cutoff.notes import (
    IMPORTED_APIS,
    NOTE_MODEL,
    SCOPE_FAILURES,
    SCOPE_IMPORTED,
    SCOPE_USED,
    TAG_TYPE_CHECKED,
    Note,
    bullet_is_grounded,
    clean_bullet,
    deps_hash,
    diff_note,
    diff_notes,
    render_block,
    render_legacy_block,
)
from since_cutoff.project import Dependency, FileUse, Project
from since_cutoff.providers import KNOWN_PROVIDERS, Provider, check_spec, split_spec
from since_cutoff.pypi import SOURCE_SCHEMA, PyPI, Release, SourceTree, is_placeholder
from since_cutoff.selection import (
    NAME_MATCH,
    OLD_FORM,
    PATH_MATCH,
    USES_API,
    Use,
    collapse,
    imported_rank,
    ordered,
    project_rank,
    select,
    used_names,
    uses,
)
from since_cutoff.taskfile import TaskFile

log = logging.getLogger(__name__)
T = TypeVar("T")

PASS = "pass"
STALE = "stale"
WRONG = "wrong"
DEPRECATED = "deprecated"
UNTOUCHED = "untouched"
OFFTASK = "off-task"
INVALID = "invalid"
ERROR = "error"
VALID_OUTCOMES = (PASS, STALE, WRONG, DEPRECATED)
EXCLUDED_OUTCOMES = (UNTOUCHED, OFFTASK, INVALID, ERROR)

CLAUDE_FAMILIES = ("sonnet", "opus", "haiku", "fable")
CLAUDE_ALIASES = (*CLAUDE_FAMILIES, "default", "opusplan")
DEPENDENCY_DEPTH = 2
MAX_DEPENDENCIES = 40
SOURCE_JOBS = 8  # parallel source downloads

# Package status after the scan.
CHANGED = "changed"
UNCHANGED = "unchanged"
KNOWN = "known"
NEW = "new"
SKIPPED = "skipped"
# A release at the cutoff with files this small and nothing in its modules only reserved the
# name (see Engine._placeholder); a compiled extension would not fit.
_PLACEHOLDER_BYTES = 64 * 1024
# Distributions a release's modules take their names from with ``from x import *`` (mcp 2.3's
# ``mcp/types/__init__.py`` is ``from mcp_types import *``: see star_imports_from_outside),
# fetched per release at most; a package that re-exports more than this is a bundle.
SIBLING_LIMIT = 4
# ``from mcp_types import *``, at the start of a line (not a relative import): how a module that
# does not parse is read (_star_imported).
_STAR_IMPORT = re.compile(r"^[ \t]*from[ \t]+([A-Za-z_]\w*(?:\.\w+)*)[ \t]+import[ \t]+\*", re.M)
# The statements with blocks a star import may run under (_module_level); ``try*`` is 3.11's.
_TRY: tuple[type[ast.stmt], ...] = tuple(
    getattr(ast, name) for name in ("Try", "TryStar") if hasattr(ast, name)
)
# ``sync --scope imported``: the APIs noted per changed package the code imports (at least all
# those it uses), in the order of ScanResult.ranked.
# A class whose fields mirror a callable's parameters (the SDKs' ``MessageCreateParamsBase``).
_PARAMS_CLASS = re.compile(r"Params\w*$")
# A name private to a library, in a type annotation: ``_ClassScanMapperConfig``,
# ``orm._ClassScanMapperConfig`` (not a dunder).
_PRIVATE_TYPE = re.compile(r"(?:^|\W)_(?!_)[A-Za-z]\w*")


def probeable(change: APIChange) -> bool:
    """Whether ``run`` may probe a change. Not a deprecation marked by a library's own
    decorator: the type checker does not see it, so a probe could never show whether the model
    avoids it. Not a switched dependency (DEPENDENCY_SWITCHED): the task templates need a
    callable and a parameter to exercise, which it has not (a task that builds the client with
    a custom HTTP client or timeout, type-checked with the new library installed, is for
    later)."""
    return not change.deprecated_by and change.kind != DEPENDENCY_SWITCHED


def _not_worth_a_note(change: APIChange, lost: set[str]) -> bool:
    """For ``sync --scope imported``, a change the code does not use that a note would only
    spend a slot on: a field of a params class that mirrors a parameter a callable of the same
    package lost (``lost``; the callable's note says it), or a change to an internal hook,
    one of whose parameters has a type private to the library (sqlalchemy's
    ``MappedColumn.declarative_scan_for_composite(decl_scan: _ClassScanMapperConfig)``)."""
    if (
        change.owner
        and not change.parameter
        and change.name in lost
        and _PARAMS_CLASS.search(change.owner)
    ):
        return True
    if not change.parameter:
        return False
    new_side = change.kind in (PARAM_REQUIRED, PARAM_KEYWORD_ONLY, PARAM_POSITIONAL_ONLY)
    signature = (change.new_signature if new_side else change.old_signature) or ""
    name = re.escape(change.parameter.lstrip("*"))
    annotation = re.search(rf"(?<![\w.]){name}\s*:\s*([^,=)]*)", signature)
    return annotation is not None and _PRIVATE_TYPE.search(annotation.group(1)) is not None


def scanned_dependencies(
    project: Project,
    *,
    all_deps: bool = False,
    include: Iterable[str] = (),
    exclude: Iterable[str] = (),
) -> list[Dependency]:
    """The dependencies a scan checks: the direct ones, and a dependency of a dependency that
    the code imports itself (alembic, pinned by pip-compile "via flask-migrate"), which is used
    like a direct one; every one with ``all_deps``; only those in ``include`` when given; none
    in ``exclude``. No network: ``since-cutoff status`` needs the same list offline."""
    wanted = {canonicalize_name(s) for s in include}
    skip = {canonicalize_name(s) for s in exclude}
    imported = {canonicalize_name(m) for m in project.imported_modules}
    deps = [
        d
        for d in project.dependencies
        if d.direct or all_deps or d.key in wanted or d.key in imported
    ]
    if wanted:
        deps = [d for d in deps if d.key in wanted]
    return [d for d in deps if d.key not in skip]


def dependency_pairs(deps: Iterable[Dependency]) -> list[tuple[str, str]]:
    """What the notes block's ``deps`` hash is made of: each dependency with the version the
    project's own files give it (a lockfile, a pin, the virtual environment). One they leave
    open counts with its declared range (``numpy``: ``unpinned <2.3``), not with the release
    PyPI has today, so that ``since-cutoff status`` can compute the hash without the network;
    ``sync``, which asks PyPI, sees a new release of it."""
    return [(d.key, d.version or f"unpinned {d.specifier}".rstrip()) for d in deps]


def _claude_settings_model() -> str | None:
    """The model configured in ~/.claude/settings.json, if any."""
    return hosts.claude_settings_model(hosts.user_home() / ".claude" / "settings.json")


# ----------------------------------------------------------------- reporting
class Reporter:
    """Progress hooks. The CLI renders them with rich; tests use the silent default."""

    def stage(self, title: str, total: int | None = None) -> None: ...

    def advance(self, n: int = 1, *, label: str | None = None) -> None:
        """``n`` more steps of the stage done; ``label`` names the last one (a package)."""

    def info(self, message: str) -> None: ...

    def warn(self, message: str) -> None: ...

    def done(self) -> None: ...


# --------------------------------------------------------------- data model
@dataclass
class Settings:
    model: str = "claude-code"
    # Where ``model`` came from when --model was not given: a setting of the user's coding
    # agent (:func:`since_cutoff.hosts.detect_model`) or ``hosts.DEFAULT_SOURCE``.
    model_source: str | None = None
    cutoff: date | None = None
    # Days before the cutoff the comparison release must have been published (--cutoff-margin,
    # SINCE_CUTOFF_CUTOFF_MARGIN; 0: the latest release at the cutoff itself).
    cutoff_margin: int = DEFAULT_CUTOFF_MARGIN
    task_model: str | None = None
    max_probes: int = 30
    heldout: int = 2
    regression: int = 6
    jobs: int = 4
    all_deps: bool = False
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    max_download_mb: float = 80.0
    python_version: str | None = None
    base_url: str | None = None
    effort: str | None = "low"
    today: date = field(default_factory=date.today)
    # Tasks to use instead of calling the task writer (``run --tasks-from``).
    tasks_from: TaskFile | None = None
    # Baseline notes blocks tested next to the run's notes (``run --compare``).
    compare: list[str] = field(default_factory=list)


@dataclass
class ModelTarget:
    spec: str
    model_id: str  # empty for a cutoff given without a model
    cutoff: date
    cutoff_source: str
    info: ModelInfo | None = None
    effort: str | None = None
    # Days before the cutoff the comparison release must have been published (Settings.cutoff_margin).
    margin: int = DEFAULT_CUTOFF_MARGIN

    @classmethod
    def cutoff_only(
        cls, cutoff: date, source: str = "--cutoff", *, margin: int = DEFAULT_CUTOFF_MARGIN
    ) -> ModelTarget:
        """A cutoff date with no model behind it (``scan --cutoff`` without ``--model``)."""
        return cls("", "", cutoff, source, margin=margin)

    @property
    def compare_date(self) -> date:
        """The day the comparison release of each dependency must have been published by:
        ``margin`` days before the cutoff (:func:`since_cutoff.models.compare_date`)."""
        return compare_date(self.cutoff, self.margin)

    @property
    def compare_when(self) -> str:
        """When the release a scan compares from was the latest one, in words: ``30 days
        before the cutoff``, or ``at the cutoff`` with a margin of 0."""
        return f"{self.margin} days before the cutoff" if self.margin else "at the cutoff"

    def released_within_margin(self, p: PackageScan) -> bool:
        """Was ``p``, a dependency the scan counts as new (NEW), first released within the
        margin: after the day the scan compares from, but not after the cutoff, which is what
        a text about a new dependency says otherwise."""
        if p.status != NEW or not p.first_released:
            return False
        return self.compare_date.isoformat() < p.first_released <= self.cutoff.isoformat()


@dataclass
class PackageScan:
    name: str
    locked: str | None
    version_source: str
    direct: bool
    # Whether the project's code imports the package: set by Engine.scan from its import
    # names, so None when those are unknown (its files were not read: released before the
    # cutoff, first released after it, or not checked).
    imported: bool | None = None
    status: str = SKIPPED
    cutoff_version: str | None = None
    cutoff_version_date: str | None = None
    locked_date: str | None = None
    import_names: list[str] = field(default_factory=list)
    changes: list[APIChange] = field(default_factory=list)
    reason: str | None = None
    # NEW (no release by the cutoff): the day of its first release, from the release list the
    # scan read anyway.
    first_released: str | None = None
    # CHANGED: modules the pinned release ships compiled, without a source or a stub, that hid
    # something the release at the cutoff had: the diff did not report its removal, and could
    # not compare it (issue #52; diff_sources_with_unread); and classes whose attributes a
    # base outside the package may set, which were not compared (apidiff.UNREAD_BASE).
    unread: list[str] = field(default_factory=list)
    # CHANGED: modules of the pinned release that take their names from another distribution
    # (mcp 2.3's ``mcp/types/__init__.py`` is ``from mcp_types import *``) which could not be
    # read, with why (``"mcp-types 2.3.0, which could not be downloaded: ..."``): changes to
    # them are not reported (Engine._siblings, reexport_warning).
    reexported: dict[str, str] = field(default_factory=dict)

    @property
    def breaking(self) -> list[APIChange]:
        return [c for c in self.changes if c.kind != CHANGE_DEPRECATED]

    @property
    def deprecations(self) -> list[APIChange]:
        return [c for c in self.changes if c.kind == CHANGE_DEPRECATED]

    @property
    def distinct(self) -> list[APIChange]:
        """Each change once, under its shortest public path (:func:`selection.collapse`).

        ``changes`` lists a change once per public path that leads to it; the reports count
        and list these instead. Recomputed when ``changes`` is replaced or grows.
        """
        cached = self.__dict__.get("_distinct")
        if cached is None or cached[0] is not self.changes or cached[1] != len(self.changes):
            cached = (self.changes, len(self.changes), collapse(self.changes))
            self.__dict__["_distinct"] = cached
        return list(cached[2])

    @property
    def counts(self) -> tuple[int, int]:
        """``(breaking, deprecated)``, counted as in :attr:`distinct`, for the changes to
        public APIs (APIChange.tier); :attr:`internal` counts the others."""
        distinct = [c for c in self.distinct if c.tier != TIER_INTERNAL]
        deprecated = sum(c.kind == CHANGE_DEPRECATED for c in distinct)
        return len(distinct) - deprecated, deprecated

    @property
    def internal(self) -> int:
        """The distinct changes to internal APIs (APIChange.tier), breaking or deprecated."""
        return sum(c.tier == TIER_INTERNAL for c in self.distinct)

    def to_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items() if k != "changes" and not k.startswith("_")}
        d["changes"] = [c.to_dict() for c in self.changes]
        return d


@dataclass
class UsedAPI:
    """A changed API the project's code uses (:meth:`ScanResult.used_apis`): its package, the
    note from the API diff for it (which lists its changes), where the code uses it and how.

    ``uses`` is file-level for now (``Use.line`` is None): the lines are issue #8's.
    """

    package: PackageScan
    note: Note
    uses: list[Use]  # most specific first (selection.uses)
    forms: dict[str, str]  # change id -> OLD_FORM or USES_API (selection.form)
    match: str | None  # PATH_MATCH, or NAME_MATCH when only a name matched
    # A switched dependency (DEPENDENCY_SWITCHED): the project's own entry for the distribution
    # the package no longer requires, ``{"name", "version", "source", "direct"}`` as
    # Project.dependencies has it (a lockfile, the environment, a pin), or None when the
    # project lists no such distribution. None for any other API.
    installed: dict[str, Any] | None = None

    @property
    def changes(self) -> list[APIChange]:
        return self.note.covered

    @property
    def form(self) -> str:
        """OLD_FORM when the code uses any of its changes in the old form, else USES_API."""
        return OLD_FORM if OLD_FORM in self.forms.values() else USES_API

    @property
    def files(self) -> list[str]:
        """The files that use it, in the order of ``uses``."""
        return list(dict.fromkeys(u.file for u in self.uses if u.file))


def stale_warning(stale: dict[str, date]) -> str:
    """The scan's warning when PyPI could not be reached and release lists came from older
    cached copies (:attr:`PyPI.stale`: each package with the day its copy was fetched)."""
    listed = [f"{name} (cached {day.isoformat()})" for name, day in stale.items()]
    names = listed[0] if len(listed) == 1 else ", ".join(listed[:-1]) + " and " + listed[-1]
    one = len(listed) == 1
    return (
        f"PyPI could not be reached: the release list{'' if one else 's'} of {names} "
        f"{'is an older copy' if one else 'are older copies'} from the cache, so releases "
        f"published after {'that day' if one else 'those days'} are unknown to this scan"
    )


def unread_warning(s: PackageScan) -> str:
    """The scan's warning for a package whose diff could not compare everything
    (:attr:`PackageScan.unread`: compiled modules, classes with a base outside the package)."""
    return unread_text(s.name, s.locked or "?", s.cutoff_version or "?", s.unread)


def reexport_warning(s: PackageScan) -> str:
    """The scan's warning for a package whose pinned release fills modules with another
    distribution's names (``from mcp_types import *``) that could not be read
    (:attr:`PackageScan.reexported`): ``mcp 2.3.0: changes to mcp.types are not reported
    (re-exported from mcp-types 2.3.0, which could not be downloaded: ...)``."""
    by_reason: dict[str, list[str]] = {}
    for module, why in s.reexported.items():
        by_reason.setdefault(why, []).append(module)
    parts = [
        f"changes to {_listed(modules)} are not reported (re-exported from {why})"
        for why, modules in by_reason.items()
    ]
    return f"{s.name} {s.locked}: " + "; ".join(parts)


def _listed(items: list[str]) -> str:
    """``a``, ``a and b``, ``a, b and c``."""
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


@dataclass(frozen=True)
class Sibling:
    """A distribution that a release's modules take their names from with ``from x import *``
    and that its Requires-Dist names (mcp 2.3.0's ``mcp/types/__init__.py`` is ``from mcp_types
    import *``, and it requires ``mcp-types==2.3.0``): read next to the release, so that the
    diff compares those names too (:meth:`Engine._siblings`)."""

    name: str  # the distribution, canonical: "mcp-types"
    package: str  # the top-level package star-imported: "mcp_types"
    modules: tuple[str, ...]  # the release's modules that star-import it: "mcp.types", ...
    requirement: str  # as the release's Requires-Dist lists it: "mcp-types==2.3.0"
    version: str | None = None  # the release it resolves to; None when none matches
    problem: str | None = None  # why there is no version (PyPI could not say)

    def tag(self, side: str) -> str:
        """How a diff's cache key names this sibling, read next to the ``"old"`` or the
        ``"new"`` release: ``new:mcp-types==2.3.0`` (:meth:`Engine._diff_key`)."""
        return f"{side}:{self.name}=={self.version}"

    def record(self) -> dict[str, Any]:
        """What the store keeps of a sibling (:meth:`Engine._wanted_siblings`): everything but
        the version, which each scan resolves."""
        return {
            "name": self.name,
            "package": self.package,
            "modules": list(self.modules),
            "requirement": self.requirement,
        }

    @classmethod
    def from_record(cls, data: Mapping[str, Any]) -> Sibling:
        return cls(data["name"], data["package"], tuple(data["modules"]), data["requirement"])


def star_imports_from_outside(tree: SourceTree) -> dict[str, list[str]]:
    """For each top-level package outside ``tree`` that a module of the tree star-imports when
    it runs (``from mcp_types import *``: :func:`_star_imported`), the dotted names of the
    modules that do, in the order of their files. Not the standard library, and not a module
    the tree ships itself."""
    own = {n.split(".")[0].removesuffix("-stubs") for n in tree.import_names}
    out: dict[str, list[str]] = {}
    for top in sorted(own):
        base = tree.root / top
        files = sorted(base.rglob("*.py")) if base.is_dir() else [base.with_suffix(".py")]
        for path in files:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if "import *" not in text:
                continue
            parts = list(path.relative_to(tree.root).with_suffix("").parts)
            if parts[-1] == "__init__":
                parts.pop()
            module = ".".join(parts)
            for package in _star_imported(text):
                shipped = (tree.root / package).exists() or (tree.root / f"{package}.py").exists()
                if package in own or package in sys.stdlib_module_names or shipped:
                    continue
                modules = out.setdefault(package, [])
                if module not in modules:
                    modules.append(module)
    return out


def _star_imported(text: str) -> list[str]:
    """The top-level packages a module's text star-imports when it runs, in order: ``from x
    import *`` at module level, under an ``if``, a ``try`` or a ``with`` too, but not inside a
    function or a class, in a docstring, or under ``if TYPE_CHECKING:``, whose names a type
    checker sees and a program does not. A text that does not parse (another Python's syntax)
    is read line by line instead (``_STAR_IMPORT``)."""
    try:
        module = ast.parse(text)
    except (SyntaxError, ValueError):
        return [m.group(1).split(".")[0] for m in _STAR_IMPORT.finditer(text)]
    out: list[str] = []
    for node in _module_level(module.body):
        if not isinstance(node, ast.ImportFrom) or not node.module or node.level:
            continue
        if any(alias.name == "*" for alias in node.names):
            out.append(node.module.split(".")[0])
    return out


def _module_level(body: list[ast.stmt]) -> Iterator[ast.stmt]:
    """The statements that run when a module is imported: ``body`` and the blocks in it, but
    not a function's or a class's body, nor the ``TYPE_CHECKING`` side of an ``if``."""
    for node in body:
        yield node
        if isinstance(node, ast.If):
            checking = _type_checking(node.test)
            if checking is not True:
                yield from _module_level(node.body)
            if checking is not False:
                yield from _module_level(node.orelse)
        elif isinstance(node, _TRY):
            tried = cast(ast.Try, node)  # ast.TryStar (3.11) has the same blocks
            handlers = [handler.body for handler in tried.handlers]
            for block in (tried.body, *handlers, tried.orelse, tried.finalbody):
                yield from _module_level(block)
        elif isinstance(node, ast.With):
            yield from _module_level(node.body)


def _sibling_tags(pair: tuple[list[Sibling], list[Sibling]]) -> tuple[str, ...]:
    """How a diff's cache key names the siblings of its old and new release, when every one
    with a version is read (:meth:`Engine._diff_key`): ``("new:mcp-types==2.3.0",)``."""
    old, new = pair
    return tuple(
        sib.tag(side)
        for side, found in (("old", old), ("new", new))
        for sib in found
        if sib.version
    )


def _type_checking(test: ast.expr) -> bool | None:
    """True for ``TYPE_CHECKING`` (``typing.TYPE_CHECKING`` too), False for ``not
    TYPE_CHECKING``, None for any other test."""
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        inner = _type_checking(test.operand)
        return None if inner is None else not inner
    if isinstance(test, ast.Name):
        return True if test.id == "TYPE_CHECKING" else None
    if isinstance(test, ast.Attribute):
        return True if test.attr == "TYPE_CHECKING" else None
    return None


@dataclass
class ScanResult:
    project: Project
    target: ModelTarget
    packages: list[PackageScan]
    warnings: list[str] = field(default_factory=list)
    # The packages whose release list is an older cached copy because PyPI could not be
    # reached, with the day of each copy (PyPI.stale, this scan's packages only): its warning
    # is in ``warnings``, and here for reports that pick which warnings to show (MCP).
    stale: dict[str, date] = field(default_factory=dict)

    @property
    def changed(self) -> list[PackageScan]:
        return [p for p in self.packages if p.status == CHANGED]

    @property
    def skipped(self) -> list[PackageScan]:
        return [p for p in self.packages if p.status == SKIPPED]

    @property
    def total_changes(self) -> int:
        """Breaking changes over all packages, each counted once (see PackageScan.distinct)."""
        return sum(p.counts[0] for p in self.packages)

    def package(self, name: str) -> PackageScan:
        return next(p for p in self.packages if p.name == name)

    def uses(self, package: PackageScan) -> tuple[FileUse, ...]:
        """The files of the project's code that import the package (none when it does not).

        ``create`` in a file says nothing about a package that file does not import.
        """
        return self.project.code_use(package.import_names)

    def ranked(self, package: PackageScan, *, scope: str = SCOPE_USED) -> list[APIChange]:
        """The package's distinct changes, those the project's code uses first
        (selection.project_rank). For SCOPE_IMPORTED, the rest in the order an assistant
        writing new code against the package is most likely to run into them
        (selection.imported_rank: import paths that no longer work, then changes with a known
        replacement, then removals, then members, then deprecations): the order of ``sync
        --scope imported`` and of the changes ``scan --all`` lists per package."""
        files = self.uses(package)
        key = imported_rank if scope == SCOPE_IMPORTED else project_rank
        return sorted(package.distinct, key=lambda c: key(c, files))

    def used_changes(self, package: PackageScan) -> list[APIChange]:
        """The package's distinct changes that the project's code uses
        (:func:`selection.used_names`), in the order of :meth:`ranked`."""
        files = self.uses(package)
        if not files:
            return []
        return [c for c in self.ranked(package) if used_names(c, files)]

    def diff_notes(self, *, suggestions: bool = False) -> list[Note]:
        """A note from the API diff (:func:`notes.diff_note`) for each changed API the project's
        code uses, package by package in the order of the reports. No model is called, and
        nothing of the libraries is run."""
        return [
            note
            for p in self.changed
            for note in diff_notes(self.used_changes(p), suggestions=suggestions)
        ]

    def used_apis(self) -> list[UsedAPI]:
        """Each changed API the project's code uses, with its note, where the code uses it and
        whether in the old form (selection.form). No model is called, and nothing is run.

        Packages in the order of the reports, those the code uses in the old form first; in a
        package, the APIs used in the old form first, then in the order of :meth:`diff_notes`.
        """
        key = self._used_key()
        cached = self.__dict__.get("_used")
        if cached is not None and cached[0] == key:
            return list(cached[1])
        by_package: dict[str, list[UsedAPI]] = {}
        for note in self.diff_notes():
            p = self.package(note.change.package)
            files = self.uses(p)
            per_change = {c.id: uses(c, files) for c in note.covered}
            forms = {
                cid: OLD_FORM if any(u.form == OLD_FORM for u in its) else USES_API
                for cid, its in per_change.items()
            }
            found = ordered(u for its in per_change.values() for u in its)
            how = {u.how for u in found}
            match = PATH_MATCH if PATH_MATCH in how else NAME_MATCH if how else None
            installed = self.installed(note.change)
            by_package.setdefault(p.name, []).append(
                UsedAPI(p, note, found, forms, match, installed)
            )
        order = [p.name for p in self.packages if p.name in by_package]
        order.sort(key=lambda name: all(u.form != OLD_FORM for u in by_package[name]))
        used = [
            u for name in order for u in sorted(by_package[name], key=lambda u: u.form != OLD_FORM)
        ]
        self.__dict__["_used"] = (key, used)
        return list(used)

    def installed(self, change: APIChange) -> dict[str, Any] | None:
        """For a switched dependency, the project's entry for the distribution the package
        no longer requires (UsedAPI.installed), or None: another package may still need it,
        so it is often still installed (12 of the 36 pins of examples/ai-stack need httpx).
        Without a lockfile, the dependencies list only the declared ones: the virtual
        environment's own list (Project.installed) has what they installed."""
        if change.kind != DEPENDENCY_SWITCHED:
            return None
        wanted = canonicalize_name(change.name)
        dep = next((d for d in self.project.dependencies if d.key == wanted), None)
        if dep is None:
            version = self.project.installed.get(wanted)
            if version is None:
                return None
            return {"name": wanted, "version": version, "source": "installed", "direct": False}
        return {
            "name": dep.key,
            "version": dep.version,
            "source": dep.pinned_in or dep.source,
            "direct": dep.direct,
        }

    def _used_key(self) -> tuple[Any, ...]:
        """What :meth:`used_apis` depends on, to know when to compute it again."""
        return (
            id(self.project.files),
            len(self.project.files),
            tuple((p.name, p.status, id(p.changes), len(p.changes)) for p in self.packages),
        )

    def versions_from(self, package: PackageScan) -> str:
        """Where the version of a dependency comes from: its lockfile (``uv.lock``), the file
        that pins it (``pyproject.toml``), ``installed``, or PyPI's latest release."""
        dep = next((d for d in self.project.dependencies if d.key == package.name), None)
        if dep is not None and dep.pinned_in:
            return dep.pinned_in
        return package.version_source

    def new_imported(self) -> list[PackageScan]:
        """The dependencies first released after the cutoff that the project's code imports:
        by their import names when the scan read them (a placeholder release at the cutoff),
        else by the dependency's own name (``foo-bar`` for ``import foo_bar``)."""
        modules = {canonicalize_name(m) for m in self.project.imported_modules}
        return [
            p
            for p in self.packages
            if p.status == NEW and (p.imported if p.imported is not None else p.name in modules)
        ]

    def deps_hash(self) -> str:
        """:func:`notes.deps_hash` of every dependency scanned, at the version the project's
        own files give it (:func:`dependency_pairs`), so that ``since-cutoff status`` can
        compute it again without the network."""
        scanned = {p.name for p in self.packages}
        return deps_hash(dependency_pairs(d for d in self.project.dependencies if d.key in scanned))

    def scope_notes(
        self,
        scope: str = SCOPE_USED,
        *,
        suggestions: bool = False,
        per_package: int | None = None,
    ) -> list[Note]:
        """The notes from the API diff that a block of ``scope`` holds: for the changed APIs
        the code uses (SCOPE_USED, :meth:`diff_notes`), or, for each changed package the code
        imports, for those and the next most likely to matter, up to ``per_package``
        (:data:`IMPORTED_APIS` by default) APIs in all (SCOPE_IMPORTED, ``sync --scope
        imported``; :meth:`ranked`'s order for that scope, in which a subpackage's move counts
        once, however many modules moved: selection.package_moves). Of the APIs the code does
        not use, a params class's field that mirrors a callable's lost parameter and an
        internal hook get no note (:func:`_not_worth_a_note`)."""
        if scope != SCOPE_IMPORTED:
            return self.diff_notes(suggestions=suggestions)
        budget = per_package or IMPORTED_APIS
        notes: list[Note] = []
        for p in self.changed:
            if not p.imported:
                continue
            ranked = self.ranked(p, scope=SCOPE_IMPORTED)  # the changes the code uses first
            used = {c.api_key for c in self.used_changes(p)}
            by_key: dict[str, list[APIChange]] = {}
            for c in ranked:
                by_key.setdefault(c.api_key, []).append(c)
            # The parameters callables lost: a params class's field of the same name
            # (anthropic's ``MessageCreateParamsBase.temperature``) is a mirror of that change.
            lost = {c.parameter for c in ranked if c.parameter and c.kind == PARAM_REMOVED}
            chosen: list[str] = []
            for key, changes in by_key.items():
                if len(chosen) >= max(budget, len(used)):
                    break
                if key in used or not all(_not_worth_a_note(c, lost) for c in changes):
                    chosen.append(key)
            notes += diff_notes(
                [c for c in ranked if c.api_key in set(chosen)], suggestions=suggestions
            )
        return notes

    def notes_block(self, notes: list[Note], scope: str = SCOPE_USED) -> str | None:
        """The AGENTS.md block (format 2) for ``notes``, or None when there are none."""
        if not notes:
            return None
        return render_block(
            notes,
            model=self.target.model_id,
            cutoff=self.target.cutoff,
            version_source=self.project.version_source,
            deps=self.deps_hash(),
            scope=scope,
            margin=self.target.margin,
        )


@dataclass
class Attempt:
    change: APIChange
    task: str
    role: str  # "probe", "heldout", "regression"
    with_notes: bool
    answer: str = ""
    code: str | None = None
    outcome: str = ERROR
    errors: list[str] = field(default_factory=list)
    error: str | None = None
    cached: bool = False
    # The notes block a with-notes answer saw: the run's notes, or a --compare baseline.
    arm: str = ARM_VERIFIED

    @property
    def valid(self) -> bool:
        return self.outcome in VALID_OUTCOMES

    def to_dict(self) -> dict[str, Any]:
        return {
            "change_id": self.change.id,
            "package": self.change.package,
            "change": self.change.describe(),
            "task": self.task,
            "role": self.role,
            "with_notes": self.with_notes,
            "outcome": self.outcome,
            "errors": self.errors,
            "code": self.code,
            "error": self.error,
            "cached": self.cached,
        }


# What the notes did to one API change on its held-out tasks (ChangePairs.outcome).
CHANGE_FIXED = "fixed"
CHANGE_BROKEN = "broken"
CHANGE_NEITHER = "neither"
# Exclusion reason of a pair that lacks one of its two answers (only in hand-made results).
MISSING = "missing"
# Exclusion reason, when notes blocks are compared on the same pairs (``--compare``), of a pair
# this block could count but another block could not (its answer with the notes errored).
ANOTHER_BLOCK_ERROR = "error with another block"


@dataclass
class ChangePairs:
    """The counted held-out pairs of one API change (see :meth:`RunResult.pairing`)."""

    change_id: str
    change: str  # APIChange.describe()
    n: int = 0
    before: int = 0  # correct without the notes
    after: int = 0  # correct with the notes
    fixed: int = 0  # wrong without, correct with
    broken: int = 0  # correct without, wrong with

    def add(self, before: bool, after: bool) -> None:
        self.n += 1
        self.before += before
        self.after += after
        self.fixed += (not before) and after
        self.broken += before and not after

    @property
    def outcome(self) -> str:
        """CHANGE_FIXED when more than half of its pairs went from wrong to correct.

        That is: a strict majority of the change's counted held-out answers pass with the notes
        *and* did not pass without them. CHANGE_BROKEN is the mirror image (correct without,
        wrong with); anything else is CHANGE_NEITHER. The rule is the same for any number of
        held-out tasks; with one (``--quick``) or two (the default) it means all of them.
        """
        if self.fixed * 2 > self.n:
            return CHANGE_FIXED
        if self.broken * 2 > self.n:
            return CHANGE_BROKEN
        return CHANGE_NEITHER

    @property
    def still_correct(self) -> bool:
        """More than half of the counted pairs are correct with the notes: what the regression
        check asks of a change the model already got right (fixing it is not the question)."""
        return self.after * 2 > self.n

    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "outcome": self.outcome}


@dataclass
class Pairing:
    """Held-out answers compared task by task, without vs with the notes, grouped by change."""

    per_change: list[ChangePairs] = field(default_factory=list)
    # Pairs left out, by the outcome that left them out (see RunResult.pairing).
    excluded_reasons: dict[str, int] = field(
        default_factory=lambda: dict.fromkeys(EXCLUDED_OUTCOMES, 0)
    )

    @property
    def n(self) -> int:
        """Counted pairs (held-out tasks answered both ways)."""
        return sum(c.n for c in self.per_change)

    @property
    def before(self) -> int:
        return sum(c.before for c in self.per_change)

    @property
    def after(self) -> int:
        return sum(c.after for c in self.per_change)

    @property
    def fixed(self) -> int:
        return sum(c.fixed for c in self.per_change)

    @property
    def broken(self) -> int:
        return sum(c.broken for c in self.per_change)

    @property
    def excluded(self) -> int:
        return sum(self.excluded_reasons.values())

    @property
    def changes(self) -> int:
        """API changes with at least one counted pair."""
        return len(self.per_change)

    @property
    def changes_fixed(self) -> int:
        return sum(c.outcome == CHANGE_FIXED for c in self.per_change)

    @property
    def changes_broken(self) -> int:
        return sum(c.outcome == CHANGE_BROKEN for c in self.per_change)

    @property
    def changes_still_correct(self) -> int:
        return sum(c.still_correct for c in self.per_change)

    def to_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "before": self.before,
            "after": self.after,
            "fixed": self.fixed,
            "broken": self.broken,
            "excluded": self.excluded,
            "excluded_reasons": dict(self.excluded_reasons),
            "changes": self.changes,
            "changes_fixed": self.changes_fixed,
            "changes_broken": self.changes_broken,
            "per_change": [c.to_dict() for c in self.per_change],
        }


# Prefix of the skip reason when the task writer's model call fails (rate limit, auth, ...).
TASK_WRITER_FAILED = "task writer failed"


@dataclass
class RunResult:
    scan: ScanResult
    probes: list[Attempt] = field(default_factory=list)
    heldout: list[Attempt] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)
    block: str | None = None
    skipped_changes: list[tuple[APIChange, str]] = field(default_factory=list)
    tasks: dict[str, list[str]] = field(default_factory=dict)
    # What produced these results (versions, models, budgets): see Engine.run_settings.
    settings: dict[str, Any] = field(default_factory=dict)
    # The notes block of each arm, the run's notes first; empty unless ``--compare`` gave
    # baselines and some probe failed.
    arms: dict[str, str] = field(default_factory=dict)

    def probe_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for a in self.probes:
            counts[a.outcome] = counts.get(a.outcome, 0) + 1
        return counts

    def failing(self) -> list[Attempt]:
        return [a for a in self.probes if a.outcome in (STALE, WRONG, DEPRECATED)]

    def used_tasks(self) -> list[tuple[APIChange, list[str]]]:
        """Each probed change with the tasks it used (probe first), for ``--tasks-out``."""
        return [(a.change, self.tasks[a.change.id]) for a in self.probes]

    @property
    def all_errored(self) -> bool:
        return bool(self.probes) and all(a.outcome == ERROR for a in self.probes)

    def pairing(self, role: str, arm: str = ARM_VERIFIED, *, common: bool = False) -> Pairing:
        """Pair each held-out task's two answers, and group the pairs by API change.

        The answer with notes is the one that saw ``arm``'s block (the run's notes unless
        asked otherwise); every arm is paired against the same answer without notes.

        A pair counts only when the answer *without* notes is scorable (pass, stale, wrong or
        deprecated) and the answer *with* notes is not an error; otherwise it is excluded under
        the outcome that left it out (untouched, off-task, invalid, error). A counted with-notes
        answer is correct only when it passes: off-task, untouched or invalid with notes counts
        as wrong, so notes cannot "win" by making the model avoid the package.

        With ``common``, a pair counts only when it counts for every notes block that answered
        this role's tasks: an error with one block leaves the pair out for all of them (under
        ``error`` for that block and :data:`ANOTHER_BLOCK_ERROR` for the others). That is how
        ``--compare`` compares blocks, on the same pairs; otherwise an error on a task one block
        did not fix could hand another block a win. Without it, each block keeps every pair it
        can count, as in a run without ``--compare``.

        A change counts as fixed when more than half of its counted pairs went from wrong
        without the notes to correct with them (see :attr:`ChangePairs.outcome`).
        """
        answers: dict[tuple[str, str], dict[str | None, Attempt]] = {}
        blocks: set[str] = set()
        for a in self.heldout:
            if a.role == role:
                answers.setdefault((a.change.id, a.task), {})[a.arm if a.with_notes else None] = a
                if a.with_notes:
                    blocks.add(a.arm)
        p = Pairing()
        per_change: dict[str, ChangePairs] = {}
        for (change_id, _task), by_block in answers.items():
            without, with_notes = by_block.get(None), by_block.get(arm)
            if without is None and with_notes is None:
                continue  # another block's answer alone: nothing of this block's to pair
            reason = _excluded_because(without, with_notes)
            if (
                reason is None
                and common
                and any(_excluded_because(without, by_block.get(other)) for other in blocks)
            ):
                reason = ANOTHER_BLOCK_ERROR
            if reason is not None:
                p.excluded_reasons[reason] = p.excluded_reasons.get(reason, 0) + 1
                continue
            assert without is not None and with_notes is not None
            if change_id not in per_change:
                per_change[change_id] = ChangePairs(change_id, without.change.describe())
            per_change[change_id].add(without.outcome == PASS, with_notes.outcome == PASS)
        p.per_change = list(per_change.values())
        return p


def _excluded_because(without: Attempt | None, with_notes: Attempt | None) -> str | None:
    """Why a held-out pair is not counted (an outcome label), or None when it is."""
    if without is None or with_notes is None:
        return MISSING
    if not without.valid:
        return without.outcome
    if with_notes.outcome == ERROR:
        return ERROR
    return None


# ------------------------------------------------------------------ engine
class Engine:
    def __init__(
        self,
        settings: Settings,
        *,
        store: DiskCache,
        llm_cache: DiskCache,
        registry: ModelRegistry | None = None,
        pypi: PyPI | None = None,
        checker: Checker | None = None,
        provider_factory: Callable[[str], Provider] | None = None,
        reporter: Reporter | None = None,
        processes: bool = True,
    ) -> None:
        """``processes``: diff several packages at once in worker processes. A spawned worker
        (Windows, macOS, the MCP server) imports the program's main module again, so this needs
        its top-level code behind ``if __name__ == "__main__":``; without, diffs run here."""
        self.settings = settings
        self.processes = processes
        self.store = store
        self.llm_cache = llm_cache
        self.registry = registry or ModelRegistry(store)
        self.pypi = pypi or PyPI(store, max_download_mb=settings.max_download_mb)
        self._checker = checker
        self._provider_factory = provider_factory
        self.reporter = reporter or Reporter()
        self._providers: dict[str, Provider] = {}
        self._sources: dict[tuple[str, str], SourceTree] = {}
        self._dep_roots: dict[tuple[str, str, date | None], list[Path]] = {}
        self._vocabs: dict[tuple[str, str, date | None], frozenset[str]] = {}
        self._apis: dict[tuple[str, str, str], Any] = {}

    # -------------------------------------------------------------- helpers
    @property
    def checker(self) -> Checker:
        if self._checker is None:
            self._checker = Checker(self.store, python_version=self.settings.python_version)
        return self._checker

    def provider(self, spec: str) -> Provider:
        if spec not in self._providers:
            if self._provider_factory is None:
                from since_cutoff.providers import make_provider

                self._providers[spec] = make_provider(
                    spec, base_url=self.settings.base_url, effort=self.settings.effort
                )
            else:
                self._providers[spec] = self._provider_factory(spec)
        return self._providers[spec]

    def _map(
        self,
        fn: Callable[[Any], T],
        items: Iterable[Any],
        *,
        jobs: int | None = None,
        progress: bool = True,
    ) -> list[T]:
        """Run ``fn`` over items in a thread pool, preserving order and advancing progress.

        On Ctrl+C (or any error) queued work is cancelled instead of being run to completion.
        """
        items = list(items)
        results: list[T | None] = [None] * len(items)
        pool = ThreadPoolExecutor(max_workers=max(1, jobs or self.settings.jobs))
        try:
            futures: dict[Future[T], int] = {
                pool.submit(fn, item): i for i, item in enumerate(items)
            }
            for fut in as_completed(futures):
                results[futures[fut]] = fut.result()
                if progress:
                    self.reporter.advance()
        except BaseException:
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        pool.shutdown()
        return results  # type: ignore[return-value]

    def source(self, name: str, version: str) -> SourceTree:
        key = (name, version)
        if key not in self._sources:
            self._sources[key] = self.pypi.source(name, version)
        return self._sources[key]

    def _fetch_sources(
        self, wanted: Iterable[tuple[str, str]]
    ) -> dict[tuple[str, str], SourceTree | PackageIndexError]:
        """Download and extract several ``(name, version)`` sources at once.

        Downloads dominate a cold scan, so they run in parallel (PyPI.source locks per
        version). A version that cannot be fetched maps to its error.
        """

        def fetch(key: tuple[str, str]) -> SourceTree | PackageIndexError:
            try:
                return self.source(*key)
            except PackageIndexError as exc:
                return exc

        keys = list(dict.fromkeys(wanted))
        trees = self._map(fetch, keys, jobs=SOURCE_JOBS, progress=False)
        return dict(zip(keys, trees, strict=True))

    # --------------------------------------------------------------- model
    def resolve_target(self, *, allow_calls: bool = True) -> ModelTarget:
        """Work out which model is tested and its training cutoff.

        Claude Code aliases (``sonnet``, the CLI default, ...) move over time, so when calls are
        allowed the CLI is asked which model actually answers. Without calls (``scan``) the
        alias is mapped to the newest model of that family, and the guess is reported. The
        Anthropic API takes no aliases, so there a family name (Aider's ``model: sonnet``)
        always means the newest model of that family, reported the same way.

        When calls are allowed, the tested model is set up here, so that a missing API key or
        ``claude`` CLI fails before the scan rather than minutes into the run.

        Without calls, a model id alone (``claude-haiku-4-5``, ``gpt-5.4``, ``sonnet``) is
        enough: only its training cutoff is needed, which the model registry has.
        """
        spec = self.settings.model
        provider_name, model = split_spec(spec)
        bare = not allow_calls and model is None and provider_name not in KNOWN_PROVIDERS
        if self._provider_factory is None and not bare:
            self._check_spec(allow_calls)
        model_id = model or ""
        source_note = ""
        if bare:
            provider_name, model_id = "", spec.strip()
            if model_id.lower() in CLAUDE_ALIASES:
                model_id, source_note = self._guess_claude_code_model(model_id.lower())
        elif provider_name in ("claude-code", "claude") and (not model or model in CLAUDE_ALIASES):
            if allow_calls:
                model_id = self._discover_claude_code_model(spec)
                self.settings.model = spec = f"claude-code:{model_id}"
            else:
                model_id, source_note = self._guess_claude_code_model(model)
        elif provider_name == "anthropic" and model in CLAUDE_FAMILIES:
            model_id, source_note = self._guess_claude_code_model(model)
            if allow_calls:
                self.settings.model = spec = f"anthropic:{model_id}"
        if allow_calls:
            self.provider(spec)
        if self.settings.model_source:
            source_note = f", model from {self.settings.model_source}{source_note}"
        effort = self.settings.effort if provider_name in ("claude-code", "claude") else None
        margin = self.settings.cutoff_margin
        if self.settings.cutoff is not None:
            return ModelTarget(
                spec,
                model_id or spec,
                self.settings.cutoff,
                "--cutoff" + source_note,
                effort=effort,
                margin=margin,
            )
        info = self.registry.require(model_id, provider_name or None)
        assert info.knowledge is not None
        return ModelTarget(
            spec, info.id, info.knowledge, "models.dev" + source_note, info, effort, margin
        )

    def _check_spec(self, allow_calls: bool) -> None:
        """:func:`check_spec` for the tested model, with the vendors the registry knows (read
        only for a ``provider:model`` spec whose provider since-cutoff cannot call)."""
        provider, model = split_spec(self.settings.model)
        vendors: set[str] = set()
        if model and provider not in KNOWN_PROVIDERS:
            vendors = {*PROVIDER_ALIASES, *self.registry.data()}
        check_spec(self.settings.model, vendors=vendors, calls=allow_calls)

    def _discover_claude_code_model(self, spec: str) -> str:
        self.reporter.info("Asking Claude Code which model it runs ...")
        provider = self.provider(spec)
        provider.complete("Reply with the single word OK.", "OK?")
        resolved = provider.model_name
        if not resolved or resolved in CLAUDE_ALIASES:
            raise ProviderError(
                "could not determine which model Claude Code uses; pass --model claude-code:<model-id>"
            )
        return resolved

    def _guess_claude_code_model(self, alias: str | None) -> tuple[str, str]:
        if not alias or alias == "default":
            alias = _claude_settings_model() or "sonnet"
        if alias not in CLAUDE_ALIASES:
            return alias, ""
        # ``opusplan`` writes code with Sonnet (it plans with Opus); what ``default`` means
        # depends on the account, and Sonnet is the usual one.
        family = alias if alias in CLAUDE_FAMILIES else "sonnet"
        info = self.registry.latest_in_family("anthropic", family, self.settings.today)
        if info is None:
            raise ProviderError(
                f"cannot map the Claude Code alias '{alias}' to a model; pass --cutoff"
            )
        return info.id, f", assuming '{alias}' = {info.id}"

    # ---------------------------------------------------------------- scan
    def scan(self, project: Project, target: ModelTarget) -> ScanResult:
        warnings: list[str] = list(project.warnings)
        known = {d.key for d in project.dependencies}
        include = {canonicalize_name(s) for s in self.settings.include}
        exclude = {canonicalize_name(s) for s in self.settings.exclude}
        for name in sorted((include | exclude) - known):
            warnings.append(f"--only/--exclude: no dependency named '{name}' in this project")
        if self.settings.all_deps and not project.transitive:
            warnings.append(
                "--all-deps: without a lockfile (uv.lock, poetry.lock, ...) or a virtual "
                "environment (.venv), the transitive dependencies are unknown, so only the "
                "declared ones are checked"
            )
        deps = scanned_dependencies(
            project,
            all_deps=self.settings.all_deps,
            include=self.settings.include,
            exclude=self.settings.exclude,
        )
        for w in warnings:
            self.reporter.warn(w)

        # Those installed from git, a path or a private index are not looked up.
        queried = [d for d in deps if not d.non_pypi]
        deps_text = f"{len(queried)} dependenc{'y' if len(queried) == 1 else 'ies'}"
        self.reporter.stage(f"Checking {deps_text} on PyPI", len(queried))
        found = self._map(lambda d: self._scan_versions(d, target, project), queried, jobs=8)
        by_key = {s.name: s for s in found}
        scans = [by_key.get(d.key) or self._scan_versions(d, target, project) for d in deps]

        to_diff = [s for s in scans if s.status == CHANGED]
        if to_diff:
            # Newer than at the cutoff: whether their API changed is what the diff finds out.
            one = len(to_diff) == 1
            what = f"{len(to_diff)} package{'' if one else 's'} released after {'its' if one else 'their'}"
            self.reporter.stage(f"Diffing the API of {what} cutoff version", len(to_diff))
            locked = {d.key: d.version for d in project.dependencies if d.version}
            self._diff_all(to_diff, locked=locked, when=target.compare_date)
        self.reporter.done()
        for s in to_diff:
            if s.unread:
                warnings.append(unread_warning(s))
                self.reporter.warn(warnings[-1])
            if s.reexported:
                warnings.append(reexport_warning(s))
                self.reporter.warn(warnings[-1])
        # Only this scan's packages: a PyPI object outlives a scan in the MCP server.
        names = {s.name for s in scans}
        stale = {k: v for k, v in self.pypi.stale.items() if k in names}
        if stale:
            warnings.append(stale_warning(stale))
            self.reporter.warn(warnings[-1])
        for s in scans:
            s.imported = project.imports(s.import_names) if s.import_names else None
        # Every report lists the packages in this order: those with changes first, of those
        # the ones the code imports, then by breaking changes and deprecations.
        scans.sort(
            key=lambda s: (s.status != CHANGED, not s.imported, *(-n for n in s.counts), s.name)
        )
        return ScanResult(project, target, scans, warnings, stale)

    def _scan_versions(self, dep: Dependency, target: ModelTarget, project: Project) -> PackageScan:
        """Which release of ``dep`` the model is compared from: the newest published by
        ``target.compare_date`` (:attr:`ModelTarget.margin` days before the cutoff)."""
        scan = PackageScan(dep.key, dep.version, dep.source, dep.direct)
        if dep.non_pypi:
            scan.reason = dep.non_pypi_reason
            return scan
        try:
            if not dep.version:
                latest, scan.version_source = self._unpinned(dep, project)
                scan.locked = latest.version
            assert scan.locked is not None
            locked = self.pypi.release(dep.key, scan.locked)
            scan.locked_date = locked.uploaded.date().isoformat()
            at_cutoff = self.pypi.version_at(dep.key, target.compare_date)
            if at_cutoff is None:
                scan.status, scan.reason = NEW, "first released after the cutoff"
                # From the release list version_at just read (cached): no request of its own.
                first = min((r.uploaded for r in self.pypi.releases(dep.key)), default=None)
                scan.first_released = first.date().isoformat() if first else None
                if first is not None and target.compare_date < first.date() <= target.cutoff:
                    scan.reason = (
                        f"first released within {target.margin} days of the cutoff "
                        f"({scan.first_released})"
                    )
                return scan
            scan.cutoff_version = at_cutoff.version
            scan.cutoff_version_date = at_cutoff.uploaded.date().isoformat()
            if locked.parsed <= at_cutoff.parsed:
                scan.status = KNOWN
                return scan
            scan.status = CHANGED
        except PackageIndexError as exc:
            scan.status, scan.reason = SKIPPED, str(exc)
        return scan

    def _unpinned(self, dep: Dependency, project: Project) -> tuple[Release, str]:
        """The version a dependency that nothing pins gets, and where it comes from: the newest
        release in the range the project declares (``numpy<2.3``), for the project's Python
        when it has one (:attr:`Project.resolution_python`). With nothing in the range, the
        newest release."""
        python = project.resolution_python
        if dep.specifier or python:
            for prereleases in (False, True):
                found = self.pypi.best_match(
                    dep.key, dep.specifier, prereleases=prereleases, python=python
                )
                if found is not None:
                    where = f" matching {dep.specifier}" if dep.specifier else ""
                    return found, f"latest on PyPI{where}" + (
                        f" for Python {python}" if python else ""
                    )
        return self.pypi.latest(dep.key), "latest on PyPI"

    def diff_package(self, scan: PackageScan) -> PackageScan:
        """Diff ``scan.cutoff_version`` against ``scan.locked`` outside a project scan (cached).

        Sets ``import_names``, ``changes`` and ``status`` (CHANGED, UNCHANGED, or SKIPPED with
        a ``reason``) exactly as :meth:`scan` does for each changed dependency.
        """
        cutoff = self.settings.cutoff
        when = compare_date(cutoff, self.settings.cutoff_margin) if cutoff is not None else None
        self._diff_all([scan], when=when)
        return scan

    def _diff_all(
        self,
        scans: list[PackageScan],
        *,
        locked: Mapping[str, str] | None = None,
        when: date | None = None,
    ) -> None:
        """Diff each scan's two releases. ``locked`` (the project's dependencies by canonical
        name) and ``when`` (the comparison date) pick the versions of the distributions each
        side takes names from (:meth:`_siblings`): the locked one for the pinned release, the
        newest allowed on ``when`` for the release at the cutoff.

        The pinned release is always read: a cached diff still needs its import names. The
        release at the cutoff is read when the diff is not cached, and once to learn which
        distributions it takes names from (the diff's key names them: :meth:`_diff_key`), which
        the store then keeps (:meth:`_wanted_siblings`), so that a later scan serves the cached
        diff from the pinned release alone, with nothing else to download."""
        locked = locked or {}
        wanted: list[tuple[str, str]] = []
        for s in scans:
            assert s.cutoff_version and s.locked
            wanted.append((s.name, s.locked))
            if self._wanted_siblings(s.name, s.cutoff_version) is None:
                wanted.append((s.name, s.cutoff_version))
        trees = self._fetch_sources(wanted)

        # Served from the cache, or in need of the release at the cutoff after all.
        served: set[int] = set()
        missing: list[tuple[str, str]] = []
        for s in scans:
            assert s.cutoff_version and s.locked
            new = trees[(s.name, s.locked)]
            if (s.name, s.cutoff_version) in trees or isinstance(new, PackageIndexError):
                continue
            old_wanted = self._wanted_siblings(s.name, s.cutoff_version)
            if old_wanted is None:  # the store lost it since: read the release after all
                missing.append((s.name, s.cutoff_version))
                continue
            pair = (
                self._siblings(old_wanted, when=when),
                self._siblings(self._wanted_siblings(s.name, s.locked, new) or [], locked=locked),
            )
            key = self._diff_key(s, _sibling_tags(pair))
            cached = self.store.get("diffs", key)
            if cached is None:
                missing.append((s.name, s.cutoff_version))
                continue
            s.import_names = list(new.import_names)
            unread = self.store.get("diffs", self._unread_key(s)) or []
            self._finish(s, (cached, unread), key, store=False)
            served.add(id(s))
        trees.update(self._fetch_sources(missing))

        ready: list[tuple[PackageScan, SourceTree, SourceTree]] = []
        for s in scans:
            if id(s) in served:
                continue
            assert s.cutoff_version and s.locked
            new = trees[(s.name, s.locked)]
            if isinstance(new, PackageIndexError):
                s.status, s.reason = SKIPPED, str(new)
                self.reporter.advance(label=s.name)
                continue
            old = trees[(s.name, s.cutoff_version)]
            if self._placeholder(s, old):
                # Nothing to compare with: the model has never seen this API.
                s.status, s.import_names = NEW, list(new.import_names)
                s.reason = f"{s.cutoff_version} at the cutoff was an empty placeholder"
                self.reporter.advance(label=s.name)
                continue
            if isinstance(old, PackageIndexError):
                s.status, s.reason = SKIPPED, str(old)
                self.reporter.advance(label=s.name)
                continue
            s.import_names = list(new.import_names)
            ready.append((s, old, new))

        # The distributions either release takes names from, read next to it (Sibling).
        siblings = {
            id(s): (
                self._siblings(self._wanted_siblings(s.name, old.version, old) or [], when=when),
                self._siblings(
                    self._wanted_siblings(s.name, new.version, new) or [], locked=locked
                ),
            )
            for s, old, new in ready
        }
        sibling_trees = self._fetch_sources(
            (sib.name, sib.version)
            for pair in siblings.values()
            for side in pair
            for sib in side
            if sib.version
        )

        pending: list[
            tuple[PackageScan, SourceTree, SourceTree, list[str], list[Path], list[Path], str]
        ] = []
        for s, old, new in ready:
            roots: dict[str, list[Path]] = {"old": [], "new": []}
            fetched: list[str] = []
            for side, found in zip(("old", "new"), siblings[id(s)], strict=True):
                for sib in found:
                    tree = sibling_trees.get((sib.name, sib.version)) if sib.version else None
                    if isinstance(tree, SourceTree):
                        roots[side].append(tree.root)
                        fetched.append(sib.tag(side))
                        continue
                    if sib.problem:
                        why = f"{sib.name}, whose releases could not be listed: {sib.problem}"
                    elif sib.version is None:
                        why = f"{sib.name}, of which no release matches {sib.requirement}"
                    else:
                        why = f"{sib.name} {sib.version}, which could not be downloaded: {tree}"
                    for module in sib.modules:
                        s.reexported.setdefault(module, why)
            key = self._diff_key(s, tuple(fetched))
            cached = self.store.get("diffs", key)
            if cached is not None:
                unread = self.store.get("diffs", self._unread_key(s)) or []
                self._finish(s, (cached, unread), key, store=False)
                continue
            names = sorted(set(old.import_names) | set(new.import_names))
            pending.append((s, old, new, names, roots["old"], roots["new"], key))

        if len(pending) <= 1 or not self.processes or os.environ.get("SINCE_CUTOFF_NO_PROCESSES"):
            for s, old, new, names, old_roots, new_roots, key in pending:
                result = diff_sources_with_unread(
                    s.name,
                    old.version,
                    old.root,
                    new.version,
                    new.root,
                    names,
                    old_requires=old.requires,
                    new_requires=new.requires,
                    new_compiled=list(new.compiled),
                    old_roots=old_roots,
                    new_roots=new_roots,
                )
                self._finish(s, result, key)
            return
        workers = max(1, min(len(pending), (os.cpu_count() or 2) - 1, 6))
        pool = ProcessPoolExecutor(max_workers=workers)
        try:
            futs = {
                pool.submit(
                    diff_sources_with_unread,
                    s.name,
                    old.version,
                    old.root,
                    new.version,
                    new.root,
                    names,
                    old_requires=old.requires,
                    new_requires=new.requires,
                    new_compiled=list(new.compiled),
                    old_roots=old_roots,
                    new_roots=new_roots,
                ): (s, key)
                for s, old, new, names, old_roots, new_roots, key in pending
            }
            for fut in as_completed(futs):
                s, key = futs[fut]
                try:
                    self._finish(s, fut.result(), key)
                except Exception as exc:  # a crash in one package must not sink the run
                    s.status, s.reason = SKIPPED, f"API diff failed: {exc}"
                    self.reporter.advance(label=s.name)
        except BaseException:
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        pool.shutdown()

    def _wanted_siblings(
        self, name: str, version: str, tree: SourceTree | None = None
    ) -> list[Sibling] | None:
        """The distributions the release ``name==version`` takes names from with ``from x
        import *`` (:func:`star_imports_from_outside`) and that its Requires-Dist names, with
        no version yet (:meth:`_siblings` resolves one per scan). Read from ``tree`` once and
        kept in the store, so that a later scan knows them without the release's sources: a
        cached diff is then served from the pinned release alone. None when the store has
        nothing and there is no ``tree`` to read. A star import of a package no requirement
        names is left as it is: griffe keeps a placeholder for it, and the module's names are
        not compared, as before.

        mcp 2.3.0's ``mcp/types/__init__.py`` is ``from mcp_types import *`` and it requires
        ``mcp-types==2.3.0``: without mcp-types, the report had no entry under ``mcp.types``
        while its fields had been renamed to snake_case."""
        key = self._siblings_key(name, version)
        kept = self.store.get("diffs", key)
        if isinstance(kept, list):
            return [Sibling.from_record(r) for r in kept]
        if tree is None:
            return None
        out: list[Sibling] = []
        found = star_imports_from_outside(tree)
        if found:
            listed = requirements(tree.requires)
            texts = {**listed.extra_text, **listed.base}
            for package, modules in sorted(found.items()):
                dist = next((n for n in texts if dependency_module(n) == package), None)
                if dist is None:
                    log.debug("%s %s: no requirement provides %s", name, version, package)
                    continue
                try:
                    Requirement(texts[dist])
                except InvalidRequirement:
                    continue
                out.append(Sibling(dist, package, tuple(modules), texts[dist]))
        self.store.set("diffs", key, [s.record() for s in out])
        return out

    def _siblings(
        self,
        wanted: list[Sibling],
        *,
        when: date | None = None,
        locked: Mapping[str, str] | None = None,
    ) -> list[Sibling]:
        """``wanted`` (:meth:`_wanted_siblings`), each with the version this side is compared
        at: the project's locked version (``locked``, for the pinned release), else the newest
        release the requirement allows on ``when`` (the comparison date, for the release at the
        cutoff; today without one). At most SIBLING_LIMIT of them."""
        out: list[Sibling] = []
        for sibling in wanted[:SIBLING_LIMIT]:
            specifier = str(Requirement(sibling.requirement).specifier)
            version = (locked or {}).get(sibling.name)
            try:
                if version is None:
                    release = self.pypi.best_match(
                        sibling.name, specifier, when
                    ) or self.pypi.best_match(
                        sibling.name, specifier, when, prereleases=True, dev=False
                    )
                    version = release.version if release else None
            except PackageIndexError as exc:
                sibling = replace(sibling, problem=str(exc))
            out.append(replace(sibling, version=version))
        return out

    def _placeholder(self, s: PackageScan, old: SourceTree | PackageIndexError) -> bool:
        """Is the release at the cutoff one that only reserved the name: no modules at all
        (nvidia-cuda-runtime's sdist) or modules with nothing in them (zensical 0.0.0)?

        Only for a release of small pure-Python files: a compiled extension (ujson has no
        ``.py`` file) is code the sources do not show.
        """
        if not isinstance(old, (SourceTree, NoCodeError)):
            return False
        try:
            assert s.cutoff_version
            files = self.pypi.release(s.name, s.cutoff_version).files
        except PackageIndexError:
            return False
        if not all(
            int(f.get("size") or 0) < _PLACEHOLDER_BYTES
            and not (f["filename"].endswith(".whl") and not f["filename"].endswith("-any.whl"))
            for f in files
        ):
            return False
        return isinstance(old, NoCodeError) or is_placeholder(old)

    @staticmethod
    def _diff_key(s: PackageScan, siblings: tuple[str, ...] = ()) -> str:
        """Where a diff is cached. griffe is a range dependency: after an upgrade that reads
        sources differently, the diffs the old one made are not served again. SOURCE_SCHEMA is
        in the key with DIFF_SCHEMA because the diff reads what the source trees record (the
        compiled modules): a change to either must bump its schema. ``siblings`` are the
        distributions read next to each release (``"new:mcp-types==2.3.0"``), when any."""
        return stable_hash(
            "diff",
            DIFF_SCHEMA,
            SOURCE_SCHEMA,
            griffe_version(),
            s.name,
            s.cutoff_version,
            s.locked,
            *siblings,
        )

    @staticmethod
    def _unread_key(s: PackageScan) -> str:
        """Where a diff's :attr:`PackageScan.unread` is kept next to it. It also depends on
        what the source trees record as compiled (SOURCE_SCHEMA)."""
        return stable_hash("unread", DIFF_SCHEMA, SOURCE_SCHEMA, s.name, s.cutoff_version, s.locked)

    @staticmethod
    def _siblings_key(name: str, version: str) -> str:
        """Where the distributions a release takes names from are kept
        (:meth:`_wanted_siblings`). They are read from its sources as extracted (SOURCE_SCHEMA)
        by star_imports_from_outside, a change to which moves DIFF_SCHEMA with the diff's."""
        return stable_hash("siblings", DIFF_SCHEMA, SOURCE_SCHEMA, name, version)

    def _finish(
        self,
        s: PackageScan,
        result: tuple[list[dict[str, Any]], list[str]],
        key: str,
        *,
        store: bool = True,
    ) -> None:
        """Take a diff (its changes, and the compiled modules that hid something), computed or
        from the cache; only a finished diff sets :attr:`PackageScan.unread`, so a failed one
        warns of nothing."""
        changes, unread = result
        if store:
            self.store.set("diffs", key, changes)
            if unread:
                self.store.set("diffs", self._unread_key(s), unread)
        s.unread = list(unread)
        s.changes = [APIChange.from_dict(c) for c in changes]
        # Module metadata alone (``__version__``) is not an API change worth flagging.
        s.status = CHANGED if s.distinct else UNCHANGED
        # A diff taken from the cache is instant: in a log, only a diff that was computed says
        # how far the stage got ("diffed 3/8 (openai)"), so a warm scan's log goes straight
        # from the stage line to the results.
        self.reporter.advance(label=s.name if store else None)

    # ---------------------------------------------------------------- run
    def run_settings(self, scan: ScanResult) -> dict[str, Any]:
        """Everything besides the model's answers that a run's numbers depend on.

        Recorded in results.json and report.md, so that a run can be repeated (with the same
        tasks: ``--tasks-out`` / ``--tasks-from``) and two runs compared knowingly.
        """
        s = self.settings
        task_model = s.task_model or s.model
        claude = any(
            split_spec(spec)[0] in ("claude-code", "claude") for spec in (s.model, task_model)
        )
        return {
            "tool_version": __version__,
            "model": scan.target.spec or s.model,
            # Only without --model: the agent setting the model was read from.
            **({"model_source": s.model_source} if s.model_source else {}),
            "task_model": task_model,
            # Only Claude Code takes an effort; None there means the CLI's own default.
            "effort": (s.effort or "default") if claude else None,
            "prompt_version": prompts.PROMPT_VERSION,
            "diff_schema": DIFF_SCHEMA,
            "griffe_version": griffe_version(),
            "cutoff_margin": scan.target.margin,
            "max_probes": s.max_probes,
            "heldout": s.heldout,
            "regression": s.regression,
            "python_version": s.python_version,
            "tasks_from": s.tasks_from.source if s.tasks_from else None,
            # Only with --tasks-from: who wrote those tasks, as the file says. ``task_model``
            # above then writes the notes only.
            **({"tasks_written_by": s.tasks_from.provenance()} if s.tasks_from else {}),
            "date": s.today.isoformat(),
            # Only when given, so that a run without --compare records what it always did.
            **({"compare": list(s.compare)} if s.compare else {}),
        }

    def run(self, scan: ScanResult, *, fix: bool = True) -> RunResult:
        result = RunResult(scan, settings=self.run_settings(scan))
        changes_by_pkg = {p.name: [c for c in p.changes if probeable(c)] for p in scan.changed}
        if not changes_by_pkg:
            return result
        budget = max(0, self.settings.max_probes)
        files = {p.name: scan.uses(p) for p in scan.changed}
        candidates = select(changes_by_pkg, files, int(budget * 1.5) + 2)

        # Tasks -------------------------------------------------------------
        task_spec = self.settings.task_model or self.settings.model
        n_tasks = 1 + max(0, self.settings.heldout)
        task_lists = self._all_tasks(task_spec, candidates, n_tasks, scan)
        from_file = self.settings.tasks_from is not None
        chosen: list[APIChange] = []
        for change, (tasks, reason) in zip(candidates, task_lists, strict=True):
            if tasks and len(chosen) < budget:
                chosen.append(change)
                result.tasks[change.id] = tasks
            elif not tasks and not (from_file and len(chosen) >= budget):
                # From a file, a change past the full budget would not have been probed anyway.
                result.skipped_changes.append((change, reason or "no task"))

        # Probe -------------------------------------------------------------
        self.reporter.stage(
            f"Probing {scan.target.model_id} on {len(chosen)} API changes", len(chosen)
        )
        probes = [Attempt(c, result.tasks[c.id][0], "probe", False) for c in chosen]
        self._answer_all(probes)
        self._score(probes, scan)
        result.probes = probes
        self.reporter.done()

        failing = result.failing()
        if not fix or not failing:
            return result

        # Notes -------------------------------------------------------------
        self.reporter.stage(
            f"Writing notes for {len(failing)} failures and type-checking their examples",
            len(failing),
        )
        result.notes = self._map(lambda a: self._note(task_spec, a, scan), failing)
        result.block = block = render_block(
            result.notes,
            model=scan.target.model_id,
            cutoff=scan.target.cutoff,
            version_source=scan.project.version_source,
            deps=scan.deps_hash(),
            scope=SCOPE_FAILURES,
            margin=scan.target.margin,
        )

        # Test on held-out tasks ----------------------------------------------
        if self.settings.heldout > 0:
            blocks = {ARM_VERIFIED: block}
            compare = list(dict.fromkeys(self.settings.compare))
            if compare:
                self.reporter.stage(f"Building the baseline notes to compare: {', '.join(compare)}")
            for arm in compare:
                blocks[arm] = self._baseline_block(arm, [a.change for a in failing], scan)
            if len(blocks) > 1:
                result.arms = blocks
            # A stable pseudo-random sample, so the check is not biased towards the top ranks.
            passing = sorted(
                (a for a in result.probes if a.outcome == PASS),
                key=lambda a: stable_hash("regression", a.change.id),
            )[: max(0, self.settings.regression)]
            attempts = [
                *_verify_attempts(failing, result.tasks, "heldout", None, blocks),
                *_verify_attempts(passing, result.tasks, "regression", 1, blocks),
            ]
            used = [len(result.tasks[a.change.id]) - 1 for a in failing]
            if min(used) < self.settings.heldout:
                # What "held-out tasks per failure" really was, next to what was asked for.
                result.settings["heldout_used"] = {"fewest": min(used), "most": max(used)}
                self._warn_fewer_heldout(used)
            baselines = len(blocks) - 1
            also = f" and {baselines} baseline{'' if baselines == 1 else 's'}" if baselines else ""
            self.reporter.stage(
                f"Testing the notes{also} on {len(attempts)} held-out tasks", len(attempts)
            )
            self._answer_all(attempts, blocks=blocks)
            self._score(attempts, scan)
            result.heldout = attempts
        self.reporter.done()
        return result

    def _warn_fewer_heldout(self, used: list[int]) -> None:
        """Say so when failing changes have fewer held-out tasks than ``--heldout`` asks for.

        ``used`` is the number of held-out tasks of each failing change. They are tested on
        the held-out tasks they have (from a tasks file written with a smaller ``--heldout``, or
        from a task writer some of whose tasks were repeats), so "changes fixed" rests on fewer
        tasks there. The run records the numbers it used (``heldout_used`` in the settings).
        """
        wanted = max(0, self.settings.heldout)
        short = [h for h in used if h < wanted]
        if not short:
            return
        source = self.settings.tasks_from
        who = f"{source.source} has" if source is not None else "The task writer gave"
        advice = f"; write it with --heldout {wanted} to use {wanted}" if source is not None else ""
        self.reporter.warn(
            f"{who} fewer than {wanted} held-out tasks for {len(short)} of {len(used)} failing "
            f"API changes (as few as {min(short)}); those are tested on the ones there "
            f"are{advice}"
        )

    def _baseline_block(self, arm: str, changes: list[APIChange], scan: ScanResult) -> str:
        """The notes block of a ``--compare`` baseline for the failing changes (no model call),
        in 0.3's format, so that the baselines stay what 0.3 measured."""
        if arm == ARM_TEMPLATE:
            notes = template_notes(changes)
        elif arm == ARM_SIGNATURES:
            notes = signature_notes(changes, lambda c: self._new_api(c, scan))
        else:
            raise ValueError(f"unknown notes arm: {arm}")
        return render_legacy_block(
            notes,
            model=scan.target.model_id,
            cutoff=scan.target.cutoff,
            version_source=scan.project.version_source,
        )

    def _new_api(self, change: APIChange, scan: ScanResult) -> Any:
        """The locked version's API (griffe, loaded statically) of the import package holding
        ``change``, or None when it cannot be loaded. Each is loaded once per engine."""
        ps = scan.package(change.package)
        names = sorted(ps.import_names, key=len, reverse=True)
        name = next((n for n in names if change.path == n or change.path.startswith(n + ".")), None)
        if name is None or not ps.locked:
            return None
        key = (ps.name, ps.locked, name)
        if key not in self._apis:
            try:
                self._apis[key] = load_api(name, self.source(ps.name, ps.locked).root)
            except Exception as exc:  # griffe fails in many ways on unusual code
                log.debug("cannot load the API of %s %s: %s", *key[:2], exc)
                self._apis[key] = None
        return self._apis[key]

    # --------------------------------------------------------------- tasks
    def _all_tasks(
        self, spec: str, candidates: list[APIChange], n: int, scan: ScanResult
    ) -> list[tuple[list[str], str | None]]:
        """Tasks for each candidate change: from ``--tasks-from`` if given, else the writer."""
        source = self.settings.tasks_from
        if source is None:
            self.reporter.stage(
                f"Writing test tasks for {len(candidates)} API changes", len(candidates)
            )
            return self._map(lambda c: self._tasks(spec, c, n, scan), candidates)
        self.reporter.info(f"Using the test tasks in {source.source} (--tasks-from)")
        if source.prompt_version not in (None, prompts.PROMPT_VERSION):
            self.reporter.warn(
                f"{source.source} was written with task prompt version {source.prompt_version}; "
                f"this since-cutoff uses version {prompts.PROMPT_VERSION}"
            )
        return [source.tasks_for(c, n) for c in candidates]

    def _tasks(
        self, spec: str, change: APIChange, n: int, scan: ScanResult
    ) -> tuple[list[str], str | None]:
        import_names = scan.package(change.package).import_names
        user = prompts.task_prompt(change, import_names, n)
        key = stable_hash(
            "tasks",
            prompts.PROMPT_VERSION,
            spec,
            change.fingerprint,
            change.from_version,
            change.to_version,
            n,
        )
        cached = self.llm_cache.get("tasks", key)
        if cached is not None:
            return list(cached["tasks"]), cached.get("reason")
        try:
            text = self.provider(spec).complete(prompts.TASK_SYSTEM, user).text
        except ProviderError as exc:
            return [], f"{TASK_WRITER_FAILED}: {exc}"
        tasks, reason = prompts.parse_tasks(text, change)
        tasks = prompts.distinct_tasks(tasks)[:n]  # held-out tasks must differ from the probe
        usable = prompts.parse_json_object(text) is not None
        if len(tasks) < n and not reason:
            reason = f"task writer returned {len(tasks)} usable tasks of {n}"
        if len(tasks) < min(n, 2):
            tasks = []
        if usable:  # never cache malformed replies: a retry may succeed
            self.llm_cache.set("tasks", key, {"tasks": tasks, "reason": reason, "raw": text})
        return tasks, reason

    # ------------------------------------------------------------- answers
    def _answer_all(self, attempts: list[Attempt], *, blocks: dict[str, str] | None = None) -> None:
        """Answer each task; a with-notes attempt sees the block of its arm in ``blocks``."""
        provider = self.provider(self.settings.model)

        def answer(a: Attempt) -> None:
            notes = (blocks or {})[a.arm] if a.with_notes else None
            system = prompts.solver_system(a.change.package, a.change.to_version, notes)
            key = stable_hash("answer", prompts.PROMPT_VERSION, provider.key, system, a.task)
            cached = self.llm_cache.get("answers", key)
            if cached is not None:
                a.answer, a.cached = cached["text"], True
            else:
                try:
                    a.answer = provider.complete(system, a.task).text
                except ProviderError as exc:
                    a.outcome, a.error = ERROR, str(exc)
                    return
                self.llm_cache.set(
                    "answers", key, {"text": a.answer, "task": a.task, "model": provider.key}
                )
            a.code = extract_code(a.answer)
            if a.code is None:
                a.outcome = INVALID

        self._map(answer, attempts)

    # ------------------------------------------------------------- scoring
    def _check_env(
        self, pkg: str, version: str, when: date | None, scan: ScanResult
    ) -> tuple[SourceTree, list[Path]]:
        tree = self.source(pkg, version)
        return tree, self.dependency_roots(tree, when, scan.project)

    def dependency_roots(self, tree: SourceTree, when: date | None, project: Project) -> list[Path]:
        """Sources of the package's runtime dependencies, so the checker can resolve its types.

        For the locked version, dependencies use the project's locked versions where known; for
        the cutoff version, the newest release allowed by the specifier on the cutoff date.
        """
        key = (tree.name, tree.version, when)
        if key in self._dep_roots:
            return self._dep_roots[key]
        locked = {d.key: d.version for d in project.dependencies if d.version}
        env: dict[str, str] = {k: str(v) for k, v in default_environment().items()}
        pv = self.settings.python_version or env["python_version"]
        env.update({"python_version": pv, "python_full_version": f"{pv}.0", "extra": ""})
        seen = {canonicalize_name(tree.name)}
        roots: list[Path] = []
        frontier = [tree]
        for _depth in range(DEPENDENCY_DEPTH):
            wanted: list[tuple[str, str]] = []
            for t in frontier:
                for spec in t.requires:
                    try:
                        req = Requirement(spec)
                        if req.marker is not None and not req.marker.evaluate(env):
                            continue
                    except (InvalidRequirement, InvalidMarker, ValueError, TypeError):
                        continue
                    name = canonicalize_name(req.name)
                    if name in seen:
                        continue
                    seen.add(name)
                    version = locked.get(name) if when is None else None
                    if version is None:
                        try:
                            rel = self.pypi.best_match(name, str(req.specifier), when)
                        except PackageIndexError:
                            rel = None
                        version = rel.version if rel else None
                    if version:
                        wanted.append((name, version))
            wanted = wanted[: max(0, MAX_DEPENDENCIES - len(roots))]

            def fetch(item: tuple[str, str]) -> SourceTree | None:
                try:
                    return self.source(*item)
                except PackageIndexError as exc:
                    log.debug("dependency %s==%s unavailable: %s", item[0], item[1], exc)
                    return None

            with ThreadPoolExecutor(max_workers=SOURCE_JOBS) as pool:
                trees = [t for t in pool.map(fetch, wanted) if t is not None]
            roots.extend(t.root for t in trees)
            frontier = trees
            if not frontier or len(roots) >= MAX_DEPENDENCIES:
                break
        self._dep_roots[key] = roots
        return roots

    def _vocab(self, tree: SourceTree, deps: list[Path]) -> frozenset[str]:
        key = (tree.name, tree.version, None)
        cached = self._vocabs.get(key)
        if cached is None:
            cached = self._vocabs[key] = package_vocab([tree.root, *deps])
        return cached

    def _score(self, attempts: list[Attempt], scan: ScanResult) -> None:
        by_pkg: dict[str, list[Attempt]] = {}
        for a in attempts:
            if a.code is not None and a.error is None:
                by_pkg.setdefault(a.change.package, []).append(a)
        for pkg, items in by_pkg.items():
            ps = scan.package(pkg)
            assert ps.locked and ps.cutoff_version
            snippets = {f"{i}": a.code or "" for i, a in enumerate(items)}
            try:
                new_tree, new_deps = self._check_env(pkg, ps.locked, None, scan)
                old_tree, old_deps = self._check_env(
                    pkg, ps.cutoff_version, scan.target.compare_date, scan
                )
                # Both runs attribute against the union of import names, so a top-level module
                # that only the old version has still counts as the package.
                names = tuple(dict.fromkeys(new_tree.import_names + old_tree.import_names))
                new = self.checker.check(
                    new_tree, snippets, extra_roots=new_deps, import_names=names
                )
                old = self.checker.check(
                    old_tree, snippets, extra_roots=old_deps, import_names=names
                )
            except (CheckerError, PackageIndexError) as exc:
                for a in items:
                    a.outcome, a.error = ERROR, f"type check failed: {exc}"
                continue
            related = change_identifiers(ps.changes)
            vocab = self._vocab(new_tree, new_deps)
            for i, a in enumerate(items):
                a.outcome, a.errors = classify(
                    new[f"{i}"], old.get(f"{i}"), a.change, related, vocab
                )

    # --------------------------------------------------------------- notes
    def _note(self, spec: str, attempt: Attempt, scan: ScanResult) -> Note:
        change = attempt.change
        ps = scan.package(change.package)
        assert ps.locked
        feedback: str | None = None
        checked = False  # an example of the model's reached the type checker
        for _round in range(2):
            user = prompts.note_prompt(change, attempt.code or "", attempt.errors, feedback)
            key = stable_hash("note", prompts.PROMPT_VERSION, spec, user)
            cached = self.llm_cache.get("notes", key)
            if cached is not None:
                text = cached["text"]
            else:
                try:
                    text = self.provider(spec).complete(prompts.NOTE_SYSTEM, user).text
                except ProviderError:
                    break
                self.llm_cache.set("notes", key, {"text": text})
            data = prompts.parse_json_object(text) or {}
            bullet = clean_bullet(str(data.get("bullet") or ""))
            example = extract_code(str(data.get("example") or "")) or str(data.get("example") or "")
            if not bullet or not example.strip():
                feedback = "the reply was missing the bullet or the example"
                continue
            try:
                tree, deps = self._check_env(change.package, ps.locked, None, scan)
                check = self.checker.check(tree, {"ex": example}, extra_roots=deps)["ex"]
            except (CheckerError, PackageIndexError):
                break
            checked = True
            problems = [d.short() for d in check.api_errors + check.deprecations]
            if not check.syntax_ok:
                problems.append("the example is not valid Python")
            elif not check.uses_package:
                problems.append(f"the example does not import {change.package}")
            if problems:
                feedback = "; ".join(problems[:4])
                continue
            if not bullet_is_grounded(bullet, example, change):
                feedback = "the bullet mentions identifiers that are not used in the example"
                continue
            if len(bullet.split()) > 60:
                feedback = "the bullet is too long"
                continue
            return Note(
                change,
                bullet,
                example,
                True,
                NOTE_MODEL,
                (TAG_TYPE_CHECKED,),
                writer=spec,
                example_type_checks=True,
            )
        # No example that type-checks: the note from the diff, which says only what it shows.
        note = diff_note([change], lambda c: self._new_api(c, scan))
        note.example_type_checks = False if checked else None
        return note


def _verify_attempts(
    probes: Iterable[Attempt],
    tasks: dict[str, list[str]],
    role: str,
    limit: int | None,
    blocks: dict[str, str],
) -> list[Attempt]:
    """For each probed change, its held-out tasks (at most ``limit``) answered once without
    notes and once with each arm's block, in that order."""
    out: list[Attempt] = []
    for probe in probes:
        for task in tasks[probe.change.id][1:][:limit]:
            out.append(Attempt(probe.change, task, role, False))
            out += [Attempt(probe.change, task, role, True, arm=arm) for arm in blocks]
    return out


# ----------------------------------------------------------- classification
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def change_identifiers(changes: Iterable[APIChange]) -> frozenset[str]:
    """Identifiers that point at breaking changes (names, parameters, owners, moved names)."""
    out: set[str] = set()
    for c in changes:
        out.add(c.name)
        if c.parameter:
            out.add(c.parameter)
        if c.moved_to:
            out.add(c.moved_to.rsplit(".", 1)[-1])
        if c.name == "__init__" and c.owner:
            out.add(c.owner)
    return frozenset(n for n in out if n and not n.startswith("__"))


def touches(identifiers: frozenset[str], change: APIChange) -> bool:
    """Does the code exercise the changed API at all?

    For removals and deprecations, *not* using the API can be the correct answer, so those are
    always considered touched. For parameter changes and moves the call or import must appear.
    """
    if change.kind in (PARAM_REMOVED, PARAM_REQUIRED, PARAM_KEYWORD_ONLY, PARAM_POSITIONAL_ONLY):
        target = change.owner if change.name == "__init__" and change.owner else change.name
        return target in identifiers
    if change.kind == MOVED:
        return change.name in identifiers
    return True


_CLASS_ATTR = re.compile(r'Cannot access attribute "(\w+)" for class "([\w.]+)"')
_KWARG = re.compile(r"(?<![\w.=!<>])([A-Za-z_]\w*)\s*=(?!=)")
_KNOWLEDGE = (
    re.compile(r"is not a known attribute of module"),
    re.compile(r"is unknown import symbol"),
    re.compile(r"could not be resolved"),
    re.compile(r"No parameter named"),
    re.compile(r"Arguments? missing for parameters?"),
    re.compile(r"Expected \d+ positional argument"),
    re.compile(r"is not defined"),
    re.compile(r"Cannot instantiate abstract class"),
)


def is_knowledge_error(
    d: Diagnostic,
    *,
    fresh: bool,
    siblings: list[Diagnostic],
    vocab: frozenset[str],
    related: frozenset[str],
) -> bool:
    """Is this diagnostic about *knowing the API* (names, parameters, arity)?

    Type-strictness complaints are not: a union member lacking an attribute
    (``msg.content[0].text`` where content may be a ThinkingBlock), a lambda where a typed
    callable is expected, or an argument whose annotation got stricter. Those make code
    type-incorrect without meaning the model does not know the library version.
    """
    first = d.message.splitlines()[0] if d.message else ""
    m = _CLASS_ATTR.search(first)
    if m:
        attr = m.group(1)
        same_spot = [
            s
            for s in siblings
            if s is not d and (s.line, s.col) == (d.line, d.col) and _CLASS_ATTR.search(s.message)
        ]
        if any(_CLASS_ATTR.search(s.message).group(1) == attr for s in same_spot):  # type: ignore[union-attr]
            return False  # several union members lack it: a narrowing complaint
        return fresh or attr not in vocab
    if first.startswith("No overloads for"):
        # Overload mismatches are knowledge errors only when a keyword is unknown or changed.
        kwargs = set(_KWARG.findall(d.context))
        return bool(kwargs & related) or any(k not in vocab for k in kwargs)
    if d.rule == "reportAbstractUsage":
        return True
    return any(p.search(first) for p in _KNOWLEDGE)


def package_vocab(roots: Iterable[Path]) -> frozenset[str]:
    """Names the package defines anywhere (functions, classes, attributes, parameters)."""
    names: set[str] = set()
    pattern = re.compile(
        r"^\s*(?:async\s+)?def\s+(\w+)\s*\(([^)]*)|^\s*class\s+(\w+)|^\s*(?:self\.)?(\w+)\s*[:=]",
        re.M,
    )
    for root in roots:
        for path in root.rglob("*.py*"):
            if path.suffix not in (".py", ".pyi"):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for fn, params, cls, attr in pattern.findall(text):
                names.update(n for n in (fn, cls, attr) if n)
                names.update(re.findall(r"(\w+)\s*(?::|=|,|$)", params))
    return frozenset(names)


def classify(
    new: CheckResult,
    old: CheckResult | None,
    change: APIChange | None = None,
    related: frozenset[str] = frozenset(),
    vocab: frozenset[str] = frozenset(),
) -> tuple[str, list[str]]:
    """Label one answer by comparing checker results for the locked and the cutoff version.

    Only *knowledge errors* count (unknown names, imports and parameters, missing required
    arguments, arity), never pure type-strictness complaints. An answer is STALE when a
    statement that is clean on the cutoff version has knowledge errors on the locked version;
    WRONG when its knowledge errors are not explained by the version change.
    """
    if not new.syntax_ok:
        return INVALID, []
    if not new.uses_package:
        return OFFTASK, []
    old_ok = old is not None and old.syntax_ok
    old_statements = {d.stmt for d in old.api_errors} if old_ok and old is not None else set()
    knowledge = [
        d
        for d in new.api_errors
        if is_knowledge_error(
            d,
            fresh=old_ok and d.stmt not in old_statements,
            siblings=new.api_errors,
            vocab=vocab,
            related=related,
        )
    ]
    if knowledge:
        errors = [d.short() for d in knowledge]
        if old_ok and any(d.stmt not in old_statements for d in knowledge):
            return STALE, errors
        return WRONG, errors
    if new.deprecations:
        return DEPRECATED, [d.short() for d in new.deprecations]
    if change is not None and not touches(new.identifiers, change):
        return UNTOUCHED, []
    return PASS, []


__all__ = [
    "DEPRECATED",
    "OFFTASK",
    "PASS",
    "STALE",
    "UNTOUCHED",
    "WRONG",
    "Engine",
    "RunResult",
    "ScanResult",
    "Settings",
    "classify",
    "replace",
]
