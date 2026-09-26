"""The since-cutoff pipeline: scan -> select -> tasks -> probe -> fix -> verify.

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
5. **Fix.** For each failure a short AGENTS.md note is written and verified with the checker.
6. **Verify.** Held-out tasks are answered again with and without the notes and compared as
   pairs, and a sample of previously-correct APIs checks that the notes do not hurt.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable, Iterable
from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Any, TypeVar

from packaging.markers import InvalidMarker, default_environment
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from since_cutoff import prompts
from since_cutoff.apidiff import (
    DEPRECATED as CHANGE_DEPRECATED,
)
from since_cutoff.apidiff import (
    DIFF_SCHEMA,
    MOVED,
    PARAM_KEYWORD_ONLY,
    PARAM_POSITIONAL_ONLY,
    PARAM_REMOVED,
    PARAM_REQUIRED,
    APIChange,
    diff_sources,
)
from since_cutoff.cache import DiskCache, stable_hash
from since_cutoff.checker import Checker, CheckResult, Diagnostic, extract_code
from since_cutoff.errors import CheckerError, PackageIndexError, ProviderError
from since_cutoff.models import ModelInfo, ModelRegistry
from since_cutoff.notes import Note, bullet_is_grounded, clean_bullet, render_block, template_bullet
from since_cutoff.project import Dependency, Project
from since_cutoff.providers import Provider, check_spec, split_spec
from since_cutoff.pypi import PyPI, SourceTree
from since_cutoff.selection import select

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

CLAUDE_ALIASES = ("sonnet", "opus", "haiku", "fable", "default")
DEPENDENCY_DEPTH = 2
MAX_DEPENDENCIES = 40

# Package status after the scan.
CHANGED = "changed"
UNCHANGED = "unchanged"
KNOWN = "known"
NEW = "new"
SKIPPED = "skipped"


def _claude_settings_model() -> str | None:
    """The model configured in ~/.claude/settings.json, if any."""
    path = Path.home() / ".claude" / "settings.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8")).get("model")
    except (OSError, ValueError, AttributeError):
        return None
    return str(value).split("[", 1)[0] if value else None


# ----------------------------------------------------------------- reporting
class Reporter:
    """Progress hooks. The CLI renders them with rich; tests use the silent default."""

    def stage(self, title: str, total: int | None = None) -> None: ...

    def advance(self, n: int = 1) -> None: ...

    def info(self, message: str) -> None: ...

    def warn(self, message: str) -> None: ...

    def done(self) -> None: ...


# --------------------------------------------------------------- data model
@dataclass
class Settings:
    model: str = "claude-code"
    cutoff: date | None = None
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


@dataclass
class ModelTarget:
    spec: str
    model_id: str
    cutoff: date
    cutoff_source: str
    info: ModelInfo | None = None
    effort: str | None = None


@dataclass
class PackageScan:
    name: str
    locked: str | None
    version_source: str
    direct: bool
    imported: bool = False
    status: str = SKIPPED
    cutoff_version: str | None = None
    cutoff_version_date: str | None = None
    locked_date: str | None = None
    import_names: list[str] = field(default_factory=list)
    changes: list[APIChange] = field(default_factory=list)
    reason: str | None = None

    @property
    def breaking(self) -> list[APIChange]:
        return [c for c in self.changes if c.kind != CHANGE_DEPRECATED]

    @property
    def deprecations(self) -> list[APIChange]:
        return [c for c in self.changes if c.kind == CHANGE_DEPRECATED]

    def to_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items() if k != "changes"}
        d["changes"] = [c.to_dict() for c in self.changes]
        return d


@dataclass
class ScanResult:
    project: Project
    target: ModelTarget
    packages: list[PackageScan]
    warnings: list[str] = field(default_factory=list)

    @property
    def changed(self) -> list[PackageScan]:
        return [p for p in self.packages if p.status == CHANGED]

    @property
    def skipped(self) -> list[PackageScan]:
        return [p for p in self.packages if p.status == SKIPPED]

    @property
    def total_changes(self) -> int:
        return sum(len(p.breaking) for p in self.packages)

    def package(self, name: str) -> PackageScan:
        return next(p for p in self.packages if p.name == name)


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


@dataclass
class Pairing:
    """Held-out answers compared task by task, without vs with the notes."""

    n: int = 0
    before: int = 0
    after: int = 0
    fixed: int = 0
    broken: int = 0
    excluded: int = 0
    changes: int = 0
    changes_fixed: int = 0

    def to_dict(self) -> dict[str, int]:
        return dict(self.__dict__)


@dataclass
class RunResult:
    scan: ScanResult
    probes: list[Attempt] = field(default_factory=list)
    heldout: list[Attempt] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)
    block: str | None = None
    skipped_changes: list[tuple[APIChange, str]] = field(default_factory=list)
    tasks: dict[str, list[str]] = field(default_factory=dict)

    def probe_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for a in self.probes:
            counts[a.outcome] = counts.get(a.outcome, 0) + 1
        return counts

    def failing(self) -> list[Attempt]:
        return [a for a in self.probes if a.outcome in (STALE, WRONG, DEPRECATED)]

    @property
    def all_errored(self) -> bool:
        return bool(self.probes) and all(a.outcome == ERROR for a in self.probes)

    def pairing(self, role: str) -> Pairing:
        """Pair each held-out task's two answers.

        A pair counts only when the answer *without* notes is scorable; the with-notes answer is
        then counted as correct or not (off-task/untouched with notes counts as not correct, so
        notes cannot "win" by making the model avoid the package).
        """
        pairs: dict[tuple[str, str], dict[bool, Attempt]] = {}
        for a in self.heldout:
            if a.role == role:
                pairs.setdefault((a.change.id, a.task), {})[a.with_notes] = a
        p = Pairing()
        per_change: dict[str, list[bool]] = {}
        for (change_id, _task), arms in pairs.items():
            without, with_notes = arms.get(False), arms.get(True)
            if (
                without is None
                or with_notes is None
                or not without.valid
                or with_notes.outcome == ERROR
            ):
                p.excluded += 1
                continue
            before = without.outcome == PASS
            after = with_notes.outcome == PASS
            p.n += 1
            p.before += before
            p.after += after
            p.fixed += (not before) and after
            p.broken += before and not after
            per_change.setdefault(change_id, []).append(after)
        p.changes = len(per_change)
        p.changes_fixed = sum(
            1 for results in per_change.values() if sum(results) * 2 > len(results)
        )
        return p


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
    ) -> None:
        self.settings = settings
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
        self, fn: Callable[[Any], T], items: Iterable[Any], *, jobs: int | None = None
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

    # --------------------------------------------------------------- model
    def resolve_target(self, *, allow_calls: bool = True) -> ModelTarget:
        """Work out which model is tested and its training cutoff.

        Claude Code aliases (``sonnet``, the CLI default, ...) move over time, so when calls are
        allowed the CLI is asked which model actually answers. Without calls (``scan``) the
        alias is mapped to the newest model of that family, and the guess is reported.
        """
        if self._provider_factory is None:
            check_spec(self.settings.model)
        spec = self.settings.model
        provider_name, model = split_spec(spec)
        model_id = model or ""
        source_note = ""
        if provider_name in ("claude-code", "claude") and (not model or model in CLAUDE_ALIASES):
            if allow_calls:
                model_id = self._discover_claude_code_model(spec)
                self.settings.model = spec = f"claude-code:{model_id}"
            else:
                model_id, source_note = self._guess_claude_code_model(model)
        effort = self.settings.effort if provider_name in ("claude-code", "claude") else None
        if self.settings.cutoff is not None:
            return ModelTarget(
                spec,
                model_id or spec,
                self.settings.cutoff,
                "--cutoff" + source_note,
                effort=effort,
            )
        info = self.registry.require(model_id, provider_name)
        assert info.knowledge is not None
        return ModelTarget(spec, info.id, info.knowledge, "models.dev" + source_note, info, effort)

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
        info = self.registry.latest_in_family("anthropic", alias, self.settings.today)
        if info is None:
            raise ProviderError(
                f"cannot map the Claude Code alias '{alias}' to a model; pass --cutoff"
            )
        return info.id, f", assuming '{alias}' = {info.id}"

    # ---------------------------------------------------------------- scan
    def scan(self, project: Project, target: ModelTarget) -> ScanResult:
        warnings: list[str] = []
        known = {d.key for d in project.dependencies}
        include = {canonicalize_name(s) for s in self.settings.include}
        exclude = {canonicalize_name(s) for s in self.settings.exclude}
        for name in sorted((include | exclude) - known):
            warnings.append(f"--only/--exclude: no dependency named '{name}' in this project")
        deps = [
            d
            for d in project.dependencies
            if d.direct or self.settings.all_deps or d.key in include
        ]
        if include:
            deps = [d for d in deps if d.key in include]
        deps = [d for d in deps if d.key not in exclude]
        for w in warnings:
            self.reporter.warn(w)

        self.reporter.stage(f"Checking {len(deps)} dependencies on PyPI", len(deps))
        scans = self._map(lambda d: self._scan_versions(d, target.cutoff, project), deps, jobs=8)

        to_diff = [s for s in scans if s.status == CHANGED]
        if to_diff:
            self.reporter.stage(f"Diffing the API of {len(to_diff)} changed packages", len(to_diff))
            self._diff_all(to_diff)
        self.reporter.done()
        scans.sort(key=lambda s: (s.status != CHANGED, -len(s.changes), s.name))
        return ScanResult(project, target, scans, warnings)

    def _scan_versions(self, dep: Dependency, cutoff: date, project: Project) -> PackageScan:
        scan = PackageScan(dep.key, dep.version, dep.source, dep.direct)
        if dep.non_pypi:
            scan.reason = f"not installed from PyPI ({dep.non_pypi})"
            return scan
        try:
            if not dep.version:
                latest = self.pypi.latest(dep.key)
                scan.locked, scan.version_source = latest.version, "latest on PyPI"
            assert scan.locked is not None
            locked = self.pypi.release(dep.key, scan.locked)
            scan.locked_date = locked.uploaded.date().isoformat()
            at_cutoff = self.pypi.version_at(dep.key, cutoff)
            if at_cutoff is None:
                scan.status, scan.reason = NEW, "first released after the cutoff"
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

    def _diff_all(self, scans: list[PackageScan]) -> None:
        pending: list[tuple[PackageScan, SourceTree, SourceTree, list[str]]] = []
        for s in scans:
            assert s.cutoff_version and s.locked
            key = stable_hash("diff", DIFF_SCHEMA, s.name, s.cutoff_version, s.locked)
            try:
                new = self.source(s.name, s.locked)
                old = self.source(s.name, s.cutoff_version)
            except PackageIndexError as exc:
                s.status, s.reason = SKIPPED, str(exc)
                self.reporter.advance()
                continue
            s.import_names = list(new.import_names)
            cached = self.store.get("diffs", key)
            if cached is not None:
                self._finish(s, cached, key, store=False)
                continue
            names = sorted(set(old.import_names) | set(new.import_names))
            pending.append((s, old, new, names))

        if len(pending) <= 1 or os.environ.get("SINCE_CUTOFF_NO_PROCESSES"):
            for s, old, new, names in pending:
                result = diff_sources(s.name, old.version, old.root, new.version, new.root, names)
                self._finish(s, result, self._diff_key(s))
            return
        workers = max(1, min(len(pending), (os.cpu_count() or 2) - 1, 6))
        pool = ProcessPoolExecutor(max_workers=workers)
        try:
            futs = {
                pool.submit(
                    diff_sources, s.name, old.version, old.root, new.version, new.root, names
                ): s
                for s, old, new, names in pending
            }
            for fut in as_completed(futs):
                s = futs[fut]
                try:
                    self._finish(s, fut.result(), self._diff_key(s))
                except Exception as exc:  # a crash in one package must not sink the run
                    s.status, s.reason = SKIPPED, f"API diff failed: {exc}"
                    self.reporter.advance()
        except BaseException:
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        pool.shutdown()

    @staticmethod
    def _diff_key(s: PackageScan) -> str:
        return stable_hash("diff", DIFF_SCHEMA, s.name, s.cutoff_version, s.locked)

    def _finish(
        self, s: PackageScan, result: list[dict[str, Any]], key: str, *, store: bool = True
    ) -> None:
        if store:
            self.store.set("diffs", key, result)
        s.changes = [APIChange.from_dict(c) for c in result]
        s.status = CHANGED if s.changes else UNCHANGED
        self.reporter.advance()

    # ---------------------------------------------------------------- run
    def run(self, scan: ScanResult, *, fix: bool = True) -> RunResult:
        result = RunResult(scan)
        changes_by_pkg = {p.name: p.changes for p in scan.changed}
        if not changes_by_pkg:
            return result
        budget = max(0, self.settings.max_probes)
        candidates = select(changes_by_pkg, scan.project.identifiers, int(budget * 1.5) + 2)

        # Tasks -------------------------------------------------------------
        task_spec = self.settings.task_model or self.settings.model
        n_tasks = 1 + max(0, self.settings.heldout)
        self.reporter.stage(
            f"Writing test tasks for {len(candidates)} API changes", len(candidates)
        )
        task_lists = self._map(lambda c: self._tasks(task_spec, c, n_tasks, scan), candidates)
        chosen: list[APIChange] = []
        for change, (tasks, reason) in zip(candidates, task_lists, strict=True):
            if tasks and len(chosen) < budget:
                chosen.append(change)
                result.tasks[change.id] = tasks
            elif not tasks:
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

        # Fix ---------------------------------------------------------------
        self.reporter.stage(
            f"Writing and verifying notes for {len(failing)} failures", len(failing)
        )
        result.notes = self._map(lambda a: self._note(task_spec, a, scan), failing)
        result.block = render_block(
            result.notes,
            model=scan.target.model_id,
            cutoff=scan.target.cutoff,
            version_source=scan.project.version_source,
        )

        # Verify on held-out tasks --------------------------------------------
        if self.settings.heldout > 0:
            attempts: list[Attempt] = []
            for a in failing:
                for task in result.tasks[a.change.id][1:]:
                    attempts += [
                        Attempt(a.change, task, "heldout", False),
                        Attempt(a.change, task, "heldout", True),
                    ]
            # A stable pseudo-random sample, so the check is not biased towards the top ranks.
            passing = sorted(
                (a for a in result.probes if a.outcome == PASS),
                key=lambda a: stable_hash("regression", a.change.id),
            )[: max(0, self.settings.regression)]
            for a in passing:
                for task in result.tasks[a.change.id][1:2]:
                    attempts += [
                        Attempt(a.change, task, "regression", False),
                        Attempt(a.change, task, "regression", True),
                    ]
            self.reporter.stage(
                f"Verifying the notes on {len(attempts)} held-out answers", len(attempts)
            )
            self._answer_all(attempts, notes=result.block)
            self._score(attempts, scan)
            result.heldout = attempts
        self.reporter.done()
        return result

    # --------------------------------------------------------------- tasks
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
            return [], f"task writer failed: {exc}"
        tasks, reason = prompts.parse_tasks(text, change)
        # Held-out tasks must differ from the probe task (and from each other).
        unique: list[str] = []
        for t in tasks:
            if _normalize_task(t) not in {_normalize_task(u) for u in unique}:
                unique.append(t)
        tasks = unique[:n]
        usable = prompts.parse_json_object(text) is not None
        if len(tasks) < n and not reason:
            reason = f"task writer returned {len(tasks)} usable tasks of {n}"
        if len(tasks) < min(n, 2):
            tasks = []
        if usable:  # never cache malformed replies: a retry may succeed
            self.llm_cache.set("tasks", key, {"tasks": tasks, "reason": reason, "raw": text})
        return tasks, reason

    # ------------------------------------------------------------- answers
    def _answer_all(self, attempts: list[Attempt], *, notes: str | None = None) -> None:
        provider = self.provider(self.settings.model)

        def answer(a: Attempt) -> None:
            system = prompts.solver_system(
                a.change.package, a.change.to_version, notes if a.with_notes else None
            )
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

            with ThreadPoolExecutor(max_workers=8) as pool:
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
                    pkg, ps.cutoff_version, scan.target.cutoff, scan
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
            return Note(change, bullet, example, True, "model")
        return Note(change, template_bullet(change), None, False, "template")


def _normalize_task(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", text.lower()).split())


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
