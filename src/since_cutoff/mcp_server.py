"""MCP server: lets a coding agent ask what changed in a library since its training cutoff.

``since-cutoff mcp`` serves three read-only tools over stdio. They reuse the ``scan``
machinery (PyPI release dates, the static griffe diff, the project's lockfile), never call a
model and never import package code. stdout is the protocol channel, so nothing here prints:
diagnostics go to stderr through logging.

The tool logic lives in :class:`Tools`, which does not need the MCP SDK and is tested
directly. :func:`build_server` wraps it for the SDK, which is imported only when the server
is built, so the other subcommands never pay for it.
"""

from __future__ import annotations

import inspect
import logging
import multiprocessing
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from since_cutoff import __version__
from since_cutoff.apidiff import (
    DEPENDENCY_SWITCHED,
    DEPRECATED,
    HINT_WARNING,
    KIND_CHANGED,
    MOVED,
    PARAM_KEYWORD_ONLY,
    PARAM_POSITIONAL_ONLY,
    PARAM_REMOVED,
    PARAM_REQUIRED,
    REMOVED,
    APIChange,
)
from since_cutoff.cache import DiskCache
from since_cutoff.engine import (
    CHANGED,
    KNOWN,
    NEW,
    SKIPPED,
    UNCHANGED,
    Engine,
    ModelTarget,
    PackageScan,
    Reporter,
    ScanResult,
    Settings,
    UsedAPI,
)
from since_cutoff.errors import ModelLookupError, PackageIndexError, SinceCutoffError
from since_cutoff.models import (
    PROVIDER_ALIASES,
    ModelInfo,
    ModelRegistry,
    parse_cutoff,
)
from since_cutoff.notes import (
    SCOPE_IMPORTED,
    dependency_detail,
    rename_text,
    runtime_text,
    similar_text,
)
from since_cutoff.project import FileUse, load_project
from since_cutoff.providers import KNOWN_PROVIDERS
from since_cutoff.pypi import PyPI, Release
from since_cutoff.report import (
    LOCATIONS_SHOWN,
    api_display,
    changes_text,
    installed_text,
    places,
    use_text,
)
from since_cutoff.selection import FORM_LABELS, OLD_FORM, other_paths_text, uses_text

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

log = logging.getLogger(__name__)

# ``progress(done, total, message)``: how far a long tool call has got.
ProgressFn = Callable[[float, float | None, str], None]

SERVER_NAME = "io.github.MohammadHijjawi97/since-cutoff"
INSTRUCTIONS = """\
since-cutoff reports which public APIs of a Python library changed after your training \
cutoff, so you do not write code for an API version the project no longer has.

Call it before writing or fixing Python code that uses a third-party library whose installed \
or pinned version may have been released after your reported training cutoff (check the \
lockfile or requirements), and when code fails on an unknown name, import or parameter of a \
library.
- Pass your own model id as `model` (for example "claude-sonnet-4-5" or "gpt-5.4"). If you \
know your training cutoff instead, pass it as `cutoff` ("YYYY-MM").
- project_changes: every dependency of a project at once, at the versions it pins, starting with the changed APIs the project's code uses and a note for each. The first call on a large project can take several minutes; later calls are served from a cache.
- api_changes: one package; pass `symbol` to narrow it to the functions or classes you use.
- model_cutoff: a model's training cutoff.

Where your memory of a library disagrees, prefer the removals, moves, new required \
parameters and switched dependencies (the release requires another library instead, such as \
`httpx2` instead of `httpx`, and takes its objects) it reports; it does not see behaviour \
changes or runtime shims (a library may still \
accept a removed argument, with a warning). The tools read PyPI metadata and package sources \
statically; they never run package code or call a model. Names it calls "similar" are not \
confirmed as replacements."""

_PROVIDER_PREFIXES = frozenset({*KNOWN_PROVIDERS, *PROVIDER_ALIASES})
_CLAUDE_FAMILIES = ("sonnet", "opus", "haiku", "fable")
# Output sections, hard breaks first. The listing limit is shared between them.
_SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Dependencies switched", (DEPENDENCY_SWITCHED,)),
    ("Removed or moved", (REMOVED, MOVED)),
    ("Parameters removed", (PARAM_REMOVED,)),
    ("Parameters now required", (PARAM_REQUIRED,)),
    ("Changed kind", (KIND_CHANGED,)),
    ("Now keyword-only or positional-only", (PARAM_KEYWORD_ONLY, PARAM_POSITIONAL_ONLY)),
    ("Deprecated (still works; avoid in new code)", (DEPRECATED,)),
)
_SIGNATURE_KINDS = (PARAM_REQUIRED, PARAM_KEYWORD_ONLY, PARAM_POSITIONAL_ONLY, KIND_CHANGED)
_CAVEAT = (
    "_Static diff of the public API (names, parameters, deprecation markers). Behaviour changes "
    "behind an unchanged signature are not shown._"
)
# project_changes stays under about this many characters (some 6,000 tokens): past it, the
# remaining changed dependencies get one line each instead of their changes, and long lists of
# names are cut short, each with the count of what was left out.
PROJECT_BUDGET = 24_000
_LIST_LIMIT = 40


@dataclass(frozen=True)
class Target:
    """The cutoff a question is asked against, and how it was obtained."""

    cutoff: date
    label: str
    model: str | None = None
    note: str = ""


class Tools:
    """The MCP tools as plain methods, sharing one cache, model registry and PyPI client.

    The method docstrings are the tool descriptions the calling model reads.

    ``processes=True`` lets project_changes diff several packages at once in worker processes.
    Started with spawn (Windows, macOS, the MCP server), each worker imports the program's main
    module again, so a script needs ``if __name__ == "__main__":`` around its calls; hence it
    is off by default, and ``since-cutoff mcp`` turns it on.
    """

    def __init__(
        self,
        store: DiskCache | None = None,
        *,
        registry: ModelRegistry | None = None,
        pypi: PyPI | None = None,
        max_download_mb: float = 80.0,
        today: date | None = None,
        processes: bool = False,
    ) -> None:
        self.store = store or DiskCache()
        self.registry = registry or ModelRegistry(self.store)
        self.pypi = pypi or PyPI(self.store, max_download_mb=max_download_mb)
        self.max_download_mb = max_download_mb
        self.processes = processes
        self._today = today

    # ----------------------------------------------------------------- tools
    def model_cutoff(self, model: str) -> str:
        """Look up a model's training cutoff: library releases after it may be missing from
        its training data.

        Use it to find or confirm your own cutoff. The other two tools do not need it first:
        they take `model` and look the cutoff up themselves.

        Read-only and quick (about a second): no model is called. The cutoffs come from the
        models.dev catalogue, fetched at most once a day and cached; without network access a
        cached or bundled copy is used.

        Args:
            model: a model id, e.g. "claude-haiku-4-5", "claude-sonnet-4-5",
                "claude-opus-4-6[1m]", "gpt-5.4", "gemini-2.5-pro",
                "anthropic/claude-haiku-4.5" or "openai:gpt-5.4". Pass your own id. The
                aliases "sonnet", "opus" and "haiku" mean the newest model of that family.

        Returns one short paragraph of plain text: "<id> (<provider>, <name>): training cutoff
        YYYY-MM-DD. Released YYYY-MM-DD. Source: models.dev", then the api_changes and
        project_changes calls to make with that id. An unknown id is a tool error that lists
        close matches.
        """
        info, note = self._resolve_model(model)
        assert info.knowledge is not None
        name = f", {info.name}" if info.name.lower() != info.id.lower() else ""
        day = info.knowledge.isoformat()
        cutoff = day if info.knowledge_raw == day else f"{info.knowledge_raw} (used as {day})"
        released = f" Released {info.release_date.isoformat()}." if info.release_date else ""
        lines = [
            f"{info.id} ({info.provider}{name}): training cutoff {cutoff}.{released} "
            f"Source: {self.registry.source}.",
        ]
        if note:
            lines.append(note)
        lines.append(
            f"Library releases published after {info.knowledge.isoformat()} came after this "
            "model's reported training cutoff, so they may be missing from its training data: "
            f'check them with api_changes(package, model="{info.id}") or '
            f'project_changes(project_dir, model="{info.id}").'
        )
        return "\n".join(lines)

    def api_changes(
        self,
        package: str,
        model: str | None = None,
        cutoff: str | None = None,
        from_version: str | None = None,
        to_version: str | None = None,
        symbol: str | None = None,
        limit: int = 40,
        *,
        progress: ProgressFn | None = None,
    ) -> str:
        """List the public API changes of one PyPI package since a model's training cutoff.

        Use it before writing code against a library version that may have been released
        after your training cutoff (check the lockfile or requirements for the version), or
        when code fails
        on a name, import or parameter of that library. Pass `symbol` to see only the functions
        or classes you are about to use. For all dependencies of a project in one call, use
        project_changes.

        Compares the newest final release on or before the cutoff (or `from_version`) with
        `to_version` (default: the latest final release on PyPI).

        Read-only: downloads the two releases' wheels from PyPI (80 MB at most each, by
        default) and reads their sources statically. No package code runs and no model is
        called. The first call for a package takes a few seconds, up to a minute or two for
        very large packages; the result is cached, and later calls take about a second.

        Args:
            package: the PyPI name, e.g. "huggingface-hub" or "openai", optionally pinned:
                "anthropic==1.8.0" (the pin is used as `to_version`).
            model: your model id, e.g. "claude-haiku-4-5" or "gpt-5.4"; its training cutoff
                is looked up.
            cutoff: your training cutoff instead of `model`, as "YYYY-MM" or "YYYY-MM-DD",
                e.g. "2025-02".
            from_version: compare from this version instead of the one at the cutoff, e.g.
                "0.29.1".
            to_version: the version the project uses, e.g. "2.0.0" (default: the latest
                release on PyPI).
            symbol: only changes to what this names, e.g. "hf_hub_download",
                "Messages.create", "client.messages.create" or
                "client.chat.completions.create". Call syntax and variable names are ignored:
                the last one or two dotted parts are matched against function, class and
                parameter names (case-insensitive), and text contained in a changed path
                matches too. Separate several symbols with commas.
            limit: the most changes to list, e.g. 100 (default 40), shared between the kinds
                of change.

        One of `model`, `cutoff` or `from_version` is required.

        Returns Markdown: a "# <package> <old> -> <new>" heading; bullets with both versions
        and their release dates and the number of breaking changes and new deprecations by
        kind; then sections "Dependencies switched" (the package requires another library
        instead of one it required, such as `httpx2` instead of `httpx`, and its signatures
        take that library's objects), "Removed or moved", "Parameters removed", "Parameters
        now required", "Changed kind", "Now keyword-only or positional-only" and "Deprecated",
        one line per change with what to use instead when the diff knows it (the new import,
        the new signature, a parameter probably renamed in place, what the old version's
        deprecation text said), and names that merely look similar, labelled as not confirmed.
        A change reachable under several import paths is listed once. If the package had no
        release by the cutoff, a short note says its whole API was released after your
        reported training cutoff.
        """
        name, pinned = _split_requirement(package)
        to_version = (to_version or "").strip() or pinned
        new = self.pypi.release(name, to_version) if to_version else self.pypi.latest(name)
        new_role = "as requested" if to_version else "the latest release on PyPI"
        target: Target | None = None
        if from_version and from_version.strip():
            old = self.pypi.release(name, from_version.strip())
            old_role = "as requested"
        else:
            target = self._target(model, cutoff, needs="`from_version`")
            at_cutoff = self.pypi.version_at(name, target.cutoff)
            if at_cutoff is None:
                return _newer_than_cutoff(name, new, target)
            old = at_cutoff
            old_role = f"the newest release on or before {target.cutoff} ({target.label})"

        head = [
            f"# {name} {old.version} -> {new.version}",
            "",
            f"- From {old.version} ({_day(old)}): {old_role}",
            f"- To {new.version} ({_day(new)}): {new_role}",
        ]
        if target is not None and target.note:
            head.append(f"- {target.note}")
        if new.parsed <= old.parsed:
            head += ["", f"No changes: {new.version} is not newer than {old.version}."]
            return "\n".join(head) + "\n"

        scan = PackageScan(
            name,
            new.version,
            "requested",
            True,
            status=CHANGED,
            cutoff_version=old.version,
            cutoff_version_date=_day(old),
            locked_date=_day(new),
        )
        reporter = _Progress(progress) if progress else None
        if reporter:
            reporter.stage(f"Diffing the API of {name} {old.version} -> {new.version}", 1)
        self._engine(Settings(cutoff=target.cutoff if target else None), reporter).diff_package(
            scan
        )
        if scan.status == SKIPPED:
            raise PackageIndexError(scan.reason or f"could not diff {name}")
        if scan.status == NEW:  # the release at the cutoff was an empty placeholder
            return "\n".join([*head, "", _placeholder_note(old, new)]) + "\n"
        changes = scan.distinct
        head.append(f"- {_counts(changes)}")
        if not changes:
            return "\n".join([*head, "", "No breaking changes or new deprecations found."]) + "\n"

        wanted = (symbol or "").strip()
        if wanted and _names_package(wanted, name, changes):
            head.append(f'- symbol "{wanted}" names the package itself, so no filter was applied')
        elif wanted:
            total = len(changes)
            changes, loose = _matching(changes, wanted)
            if loose and changes:
                bare = _symbol_terms(wanted)[0][-1]
                head.append(
                    f'- nothing names "{wanted}" exactly; showing the {len(changes)} changes '
                    f"to any `{bare}`, which may be unrelated"
                )
            else:
                head.append(f'- {len(changes)} of them match symbol "{wanted}"')
            if not changes:
                return "\n".join([*head, "", _no_match(wanted, old, new, total)]) + "\n"
        shown = _share(changes, max(1, limit))
        out = [*head, *_sections(shown)]
        if len(shown) < len(changes):
            left = {id(c) for c in shown}
            rest = _counts([c for c in changes if id(c) not in left])
            out += ["", f'Not listed: {rest}. Narrow with symbol="..." or raise limit.']
        out += ["", _CAVEAT]
        return "\n".join(out) + "\n"

    def project_changes(
        self,
        project_dir: str = ".",
        model: str | None = None,
        cutoff: str | None = None,
        only: list[str] | None = None,
        limit_per_package: int = 10,
        *,
        progress: ProgressFn | None = None,
    ) -> str:
        """Check every dependency of a Python project for API changes since a training cutoff.

        Use it at the start of work on a project, or before adding code that uses its
        dependencies: one call covers every direct dependency at the version the project
        pins. For a single package, a single symbol, or a version the project does not pin,
        api_changes is faster.

        Reads the project's lockfile (uv.lock, poetry.lock, pdm.lock, pylock.toml,
        Pipfile.lock), requirements*.txt, pyproject.toml or .venv, and for each direct
        dependency compares the release that existed at the cutoff with the pinned one.
        Changes to names the project's code uses, in packages it imports, come first.

        Read-only: nothing in the project is written. Reads PyPI metadata, downloads the
        wheels of the dependencies that changed and reads them statically; no package code
        runs and no model is called. The first call on a project with many dependencies can
        take several minutes (large packages such as transformers take longest); results are
        cached, so later calls take seconds. Running `since-cutoff scan` in the project once
        fills the same cache. Reports progress while it works if the client asks for it.

        Args:
            project_dir: the project root, e.g. "/home/me/app" or ".". A relative path
                resolves against the project the client started the server for (or the
                server's working directory); pass an absolute path when unsure.
            model: your model id, e.g. "claude-haiku-4-5" or "gpt-5.4"; its training cutoff
                is looked up.
            cutoff: your training cutoff instead of `model`, as "YYYY-MM" or "YYYY-MM-DD",
                e.g. "2025-02".
            only: check only these dependencies (PyPI names), e.g. ["openai", "pydantic"].
            limit_per_package: changes listed per dependency, e.g. 20 (default 10);
                api_changes lists the rest.

        One of `model` or `cutoff` is required.

        Returns Markdown: a heading with the number of dependencies checked and where their
        versions came from; bullets with the cutoff, how many of the changed APIs the project's
        code uses, and how many dependencies changed, were first released after the cutoff, are
        unchanged, or could not be checked; then "## Your code uses these changed APIs": each
        changed API the code uses, with what changed, "old form" (the code uses it as the
        release at the cutoff allowed) or "uses this API", the files that use it (at most 3),
        the note to follow with its tag ([diff]: from the static diff; [library]: the
        replacement is named in the library's own deprecation text), the runtime caveat when
        the pinned source still accepts a removed name, and similar names, not confirmed as
        replacements; then one "## <package> <old> (<date>) -> <new> (<date>)" section per
        changed dependency, those the code imports first, with its counts and its top changes:
        first "Touching names your code uses" (each marked "[your code uses ...]"), then the
        rest grouped as in api_changes; then the dependencies released after the cutoff and
        those that could not be checked. The answer stays under about 24,000 characters: past
        that, the remaining changed dependencies get one line each, under "N more with API
        changes"; pass them in `only` to see their changes.
        """
        target = self._target(model, cutoff)
        project = load_project(_project_root(project_dir))
        settings = Settings(
            model=target.model or "cutoff",
            cutoff=target.cutoff,
            include=[s for s in (only or []) if s.strip()],
            python_version=project.python_version,
            today=self._now(),
        )
        model_target = ModelTarget(settings.model, target.model or "", target.cutoff, target.label)
        # Two stages: look every dependency up, then diff the ones that changed.
        reporter = _Progress(progress, stages=2) if progress else None
        scan = self._engine(settings, reporter).scan(project, model_target)
        return _render_project(scan, target, settings.include, max(1, limit_per_package))

    # --------------------------------------------------------------- helpers
    def _now(self) -> date:
        return self._today or date.today()

    def _engine(self, settings: Settings, reporter: Reporter | None = None) -> Engine:
        settings.max_download_mb = self.max_download_mb
        return Engine(
            settings,
            store=self.store,
            llm_cache=self.store,
            registry=self.registry,
            pypi=self.pypi,
            reporter=reporter,
            processes=self.processes,
        )

    def _target(self, model: str | None, cutoff: str | None, *, needs: str = "") -> Target:
        model = (model or "").strip() or None
        if cutoff and cutoff.strip():
            try:
                when = parse_cutoff(cutoff)
            except ValueError as exc:
                raise SinceCutoffError(f"cutoff: {exc}") from None
            label = f"cutoff given as {cutoff.strip()}" + (f" for {model}" if model else "")
            return Target(when, label, model)
        if model:
            info, note = self._resolve_model(model)
            assert info.knowledge is not None
            source = self.registry.source.split(" (")[0]
            label = f"training cutoff of {info.id}, from {source}"
            return Target(info.knowledge, label, info.id, note)
        alternatives = f", or {needs}" if needs else ""
        raise SinceCutoffError(
            "pass `model` (your model id, e.g. 'claude-sonnet-4-5') or `cutoff` (your training "
            f"cutoff, 'YYYY-MM'){alternatives}"
        )

    def _resolve_model(self, model: str) -> tuple[ModelInfo, str]:
        """Find a model the way the CLI does: ids, ``provider:id`` specs and Claude aliases.

        The provider can be any the registry knows (``google:gemini-2.5-pro``,
        ``github-copilot:gpt-5.4``), not only one since-cutoff can call.
        """
        spec = model.strip()
        if not spec:
            raise ModelLookupError("`model` is empty: pass a model id such as 'claude-sonnet-4-5'")
        prefix, _, rest = spec.partition(":")
        prefix = prefix.strip().lower()
        provider: str | None = None
        model_id = spec
        if rest and (prefix in _PROVIDER_PREFIXES or prefix in self.registry.data()):
            provider, model_id = prefix, rest.strip()
        note = ""
        family = model_id.lower()
        if family in _CLAUDE_FAMILIES and provider in (None, "claude-code", "claude", "anthropic"):
            info = self.registry.latest_in_family("anthropic", family, self._now())
            if info is not None:
                note = (
                    f"'{model_id}' is an alias: assuming the newest {family} model, {info.id}. "
                    "Pass your exact model id for an exact cutoff."
                )
        else:
            info = self.registry.lookup(model_id, provider)
        if info is None:
            close = self._close_models(model_id)
            hint = f" Close matches: {', '.join(close)}." if close else ""
            raise ModelLookupError(
                f"unknown model '{spec}' ({self.registry.source}).{hint} Pass the exact id, or "
                "pass `cutoff` ('YYYY-MM', your training cutoff) instead."
            )
        if info.knowledge is None:
            raise ModelLookupError(
                f"{self.registry.source} lists '{info.id}' without a training cutoff. Pass "
                "`cutoff` ('YYYY-MM') instead."
            )
        return info, note

    def _close_models(self, query: str, n: int = 8) -> list[str]:
        return self.registry.close_matches(query, n)


# ------------------------------------------------------------------ rendering
def change_line(change: APIChange) -> str:
    """One actionable line: what changed, and what to use instead when the diff knows it.

    Names that merely look similar are labelled as not confirmed (and left out where the
    library says there is no replacement); a parameter renamed in place is a probable rename.
    A switched dependency says where its types are now taken (notes.dependency_detail) and
    where the pinned version's source still names the old one.
    """
    if change.kind == DEPENDENCY_SWITCHED:  # the counts and places the note leaves out
        parts = [change.describe(versioned=False), dependency_detail(change)]
        runtime = runtime_text([change])
        if runtime:
            parts.append(runtime.rstrip("."))
        return _clip("; ".join(parts), 1500)
    parts = [change.describe(versioned=False)]
    if change.kind == MOVED and change.moved_to:
        module, _, name = change.moved_to.rpartition(".")
        parts.append(f"import it with `from {module} import {name}`")
    else:
        found = rename_text(change) or similar_text(change)
        if found:
            parts.append(found)
    signature = change.new_signature
    if signature and change.kind in _SIGNATURE_KINDS and signature.strip() != change.name:
        parts.append(f"now `{_clip(signature, 160)}`")
    if change.hint and change.kind != DEPRECATED:
        said = (
            "the old version warned" if change.hint_source == HINT_WARNING else "the old docs said"
        )
        parts.append(f"{said}: {_clip(change.hint.rstrip(' .;'), 200)}")
    runtime = runtime_text([change])
    if runtime:
        parts.append(runtime.rstrip("."))
    if change.occurrences > 1:
        parts.append(other_paths_text(change))
    return _clip("; ".join(parts), 480)


def _section_of(change: APIChange) -> int:
    return next((i for i, (_, kinds) in enumerate(_SECTIONS) if change.kind in kinds), 0)


def _sections(
    changes: list[APIChange], files: Sequence[FileUse] = (), level: str = "##"
) -> list[str]:
    """Markdown sections in hard-breaks-first order, keeping the given order inside each.

    With ``files`` (the files of the project's code that import the package), the changes
    that touch names the code uses come first, in their own section and in the given order.
    """
    marks = {id(c): uses_text(c, files) for c in changes} if files else {}
    out: list[str] = []
    hits = [c for c in changes if marks.get(id(c))]
    if hits:
        lines = [f"- {change_line(c)} [your code uses {marks[id(c)]}]" for c in hits]
        out += ["", f"{level} Touching names your code uses", "", *lines]
    for i, (title, _) in enumerate(_SECTIONS):
        lines = [
            f"- {change_line(c)}" for c in changes if _section_of(c) == i and not marks.get(id(c))
        ]
        if lines:
            out += ["", f"{level} {title}", "", *lines]
    return out


def _render_project(scan: ScanResult, target: Target, only: list[str], limit: int) -> str:
    project = scan.project
    by_status: dict[str, list[PackageScan]] = {}
    for p in scan.packages:
        by_status.setdefault(p.status, []).append(p)
    changed = by_status.get(CHANGED, [])  # in the order of Engine.scan: imported ones first
    quiet = sorted(by_status.get(KNOWN, []) + by_status.get(UNCHANGED, []), key=lambda p: p.name)
    new, skipped = by_status.get(NEW, []), by_status.get(SKIPPED, [])
    total = _plural(len(scan.packages), "dependency", "dependencies")
    checked = len(scan.packages) - len(skipped)
    out = [
        f"# {project.root.name}: {f'{checked} of {total}' if skipped else total} checked "
        f"(versions from {project.version_source})",
        "",
        f"- Cutoff {target.cutoff.isoformat()} ({target.label})",
    ]
    if target.note:
        out.append(f"- {target.note}")
    out += [f"- Warning: {w}" for w in project.warnings]  # (`only` gets its own line below)
    out.append(
        f"- API changed after the cutoff: {len(changed)}; first released after it: {len(new)}; "
        f"unchanged or older: {len(quiet)}; not checked: {len(skipped)}"
    )
    known = {canonicalize_name(d.name) for d in project.dependencies}
    missing = sorted({canonicalize_name(s) for s in only} - known)
    if missing:
        out.append(f"- `only` names no dependency of this project: {', '.join(missing)}")
    out += _used_section(scan)

    tail: list[str] = []
    if new:
        tail += [
            "",
            "## First released after the cutoff (so they may be missing from your training data)",
            "",
        ]
        tail += _capped(
            [
                f"- {p.name} {p.locked or ''} ({p.locked_date or 'date unknown'})"
                + (f"; {p.reason}" if p.cutoff_version and p.reason else "")
                for p in new
            ]
        )
    if skipped:
        tail += ["", "## Could not be checked", ""]
        tail += _capped(
            [f"- {p.name}: {_clip(p.reason or 'unknown reason', 160)}" for p in skipped]
        )
    if quiet:
        names = [f"{p.name} {p.locked}" for p in quiet]
        rest = f", and {len(names) - _LIST_LIMIT} more" if len(names) > _LIST_LIMIT else ""
        tail += [
            "",
            "Not listed (released before the cutoff, or no breaking change since): "
            + ", ".join(names[:_LIST_LIMIT])
            + rest,
        ]
    if changed:
        tail += ["", _CAVEAT]

    # The changed dependencies in full while they fit the budget, keeping room for one line
    # each for the rest.
    room = PROJECT_BUDGET - _size(out) - _size(tail)
    shown = 0
    for i, p in enumerate(changed):
        section = _package_section(scan, p, limit)
        if _size(section) + _size(_brief(changed[i + 1 :])) > room:
            break
        out += section
        room -= _size(section)
        shown += 1
    return "\n".join(out + _brief(changed[shown:]) + tail) + "\n"


def _used_section(scan: ScanResult) -> list[str]:
    """project_changes' first section: each changed API the project's code uses, with where
    (at most LOCATIONS_SHOWN files; the lines are issue #8's) and its note, in at most half of
    PROJECT_BUDGET, then the dependencies first released after the cutoff that the code
    imports; the bullet before it says how many APIs there are."""
    used = scan.used_apis()
    imported = [
        f"- Your code imports {p.name} {p.locked}, first released "
        f"{p.first_released or 'after the cutoff'}: released after your reported training "
        "cutoff, so its whole API may be missing from your training data."
        for p in scan.new_imported()
    ]
    if not used:
        none = ["- Your code uses none of the APIs that changed after the cutoff"]
        return [*(none if scan.changed else []), *imported]
    old = sum(u.form == OLD_FORM for u in used)
    out = [
        f"- Your code uses {_plural(len(used), 'API')} that changed after the cutoff, "
        f"{old or 'none'} in the old form (as the release at the cutoff allowed; a static name "
        "match)",
        "",
        "## Your code uses these changed APIs",
    ]
    room = PROJECT_BUDGET // 2
    for i, u in enumerate(used):
        entry = _used_entry(scan, u)
        if _size(entry) > room:
            out += ["", f"- ... and {len(used) - i} more: pass `only` to see them"]
            break
        out += entry
        room -= _size(entry)
    for line in imported:
        out += ["", line]
    return out


def _used_entry(scan: ScanResult, u: UsedAPI) -> list[str]:
    """One used API in project_changes."""
    p = u.package
    _, change = changes_text(u.changes, code=True)
    shown = places(u)
    wheres = [f"{w} ({use_text(here, code=True)})" for w, here in shown[:LOCATIONS_SHOWN]]
    if len(shown) > LOCATIONS_SHOWN:
        wheres.append(f"and {len(shown) - LOCATIONS_SHOWN} more")
    out = [
        "",
        f"- `{api_display(u)}` ({p.name} {p.cutoff_version} -> {p.locked}): {change} "
        f"[{FORM_LABELS[u.form]}]",
        f"  - used in {'; '.join(wheres) or 'your code'}",
        f"  - note: {_clip(u.note.line, 1200)}",
    ]
    if u.note.change.kind == DEPENDENCY_SWITCHED:
        out.append(f"  - places: {_clip(dependency_detail(u.note.change), 1500)}")
    installed = installed_text(scan, u)
    if installed:
        out.append(f"  - installed: {installed}")
    runtime = runtime_text(u.changes)
    if runtime:
        out.append(f"  - runtime: {runtime}")
    if u.note.not_confirmed:
        names = ", ".join(f"`{n}`" for n in u.note.not_confirmed[:10])
        out.append(f"  - similar names in {p.locked}, not confirmed as replacements: {names}")
    return out


def _brief(packages: list[PackageScan]) -> list[str]:
    """One line for each changed dependency project_changes has no room to show in full."""
    if not packages:
        return []
    example = ", ".join(f'"{p.name}"' for p in packages[:3])
    return [
        "",
        f"## {len(packages)} more with API changes (left out to keep this answer short)",
        "",
        *_capped([_package_line(p) for p in packages]),
        "",
        f"Their changes: project_changes(..., only=[{example}]) or api_changes(package, "
        "from_version=..., to_version=...).",
    ]


def _package_section(scan: ScanResult, p: PackageScan, limit: int) -> list[str]:
    """One changed dependency in project_changes: its versions, counts and top changes."""
    uses = " Your code imports it." if p.imported else ""
    ranked = scan.ranked(p, scope=SCOPE_IMPORTED)
    breaking, deprecated = p.counts
    out = [
        "",
        f"## {p.name} {p.cutoff_version} ({p.cutoff_version_date}) -> {p.locked} ({p.locked_date})",
        "",
        f"{breaking} breaking, {deprecated} deprecated.{uses}",
        *_sections(ranked[:limit], scan.uses(p), level="###"),
    ]
    if len(ranked) > limit:
        out += [
            "",
            f'{len(ranked) - limit} more: api_changes("{p.name}", '
            f'from_version="{p.cutoff_version}", to_version="{p.locked}", symbol="...")',
        ]
    return out


def _package_line(p: PackageScan) -> str:
    breaking, deprecated = p.counts
    uses = "; your code imports it" if p.imported else ""
    return (
        f"- {p.name} {p.cutoff_version} -> {p.locked}: {breaking} breaking, "
        f"{deprecated} deprecated{uses}"
    )


def _capped(lines: list[str]) -> list[str]:
    """At most :data:`_LIST_LIMIT` list items, then how many were left out."""
    if len(lines) <= _LIST_LIMIT:
        return lines
    return [*lines[:_LIST_LIMIT], f"- ... and {len(lines) - _LIST_LIMIT} more"]


def _size(lines: list[str]) -> int:
    return sum(len(line) + 1 for line in lines)


def _placeholder_note(old: Release, new: Release) -> str:
    return (
        f"{old.version} was an empty placeholder that only reserved the name: the API of "
        f"{new.version} was released after your reported training cutoff, so it may be missing "
        "from your training data. Read its documentation or source before using it."
    )


def _newer_than_cutoff(name: str, latest: Release, target: Target) -> str:
    return (
        f"# {name}: no release on or before the cutoff\n\n"
        f"{name} had no release on or before {target.cutoff.isoformat()} "
        f"({target.label}); {latest.version} was published {_day(latest)}. Its whole API was "
        "released after your reported training cutoff, so it may be missing from your training "
        "data: read its documentation or source before using it.\n"
    )


def _share(changes: list[APIChange], limit: int) -> list[APIChange]:
    """Up to ``limit`` changes, shared round-robin between the sections.

    Without it, dozens of removals would crowd out every parameter change (and the
    parameter changes of the most-used functions are often the ones that matter).
    """
    queues = [[c for c in changes if _section_of(c) == i] for i in range(len(_SECTIONS))]
    picked: list[APIChange] = []
    while len(picked) < limit and any(queues):
        for queue in queues:
            if queue and len(picked) < limit:
                picked.append(queue.pop(0))
    return picked


def _counts(changes: list[APIChange]) -> str:
    breaking = sum(c.kind != DEPRECATED for c in changes)
    per_section = [0] * len(_SECTIONS)
    for c in changes:
        per_section[_section_of(c)] += 1
    detail = ", ".join(
        f"{title.split(' (')[0].lower()} {n}"
        for (title, _), n in zip(_SECTIONS, per_section, strict=True)
        if n
    )
    deprecations = len(changes) - breaking
    total = f"{_plural(breaking, 'breaking change')}, {_plural(deprecations, 'new deprecation')}"
    return f"{total} ({detail})" if detail else total


_CALL = re.compile(r"\([^()]*\)")


def _symbol_terms(symbol: str, *, keep_case: bool = False) -> list[list[str]]:
    """``"await client.messages.create(model=m)"`` -> ``[["client", "messages", "create"]]``.

    Call syntax, backticks and whitespace are dropped, and commas separate several symbols.
    Segments are lower-cased unless ``keep_case``.
    """
    text = symbol.replace("`", "")
    while True:  # innermost parentheses first, so nested calls go too
        stripped = _CALL.sub("", text)
        if stripped == text:
            break
        text = stripped
    text = re.sub(r"\([^,]*", "", text)  # an unclosed call
    text = text.replace(")", "")  # and one closed once too often: "create(model=m))"
    terms = []
    for term in text.split(","):
        term = re.sub(r"^\s*await\s+", "", term)
        term = re.sub(r"\s+", "", term)
        segments = [s for s in (term if keep_case else term.lower()).split(".") if s]
        if segments:
            terms.append(segments)
    return terms


def _names(change: APIChange, segments: list[str], *, strict: bool) -> bool:
    """Whether the ``segments`` of a symbol name the change (case-insensitive).

    One segment matches the changed name, its class or its parameter. Strictly, two segments
    must be a class and its member (``messages.create`` for ``Messages.create``), a callable
    and its parameter, a module and a module-level name, or anything and a class (then every
    member of the class matches). A removed or moved object also matches when an earlier
    segment names it: without ``Client.completions``, every ``client.completions.<x>`` call
    breaks. Other earlier segments, such as a variable holding a client, are ignored.

    A switched dependency matches a symbol that ends in one of its terms (the two
    distributions' modules, the switched type names and parameters, the switched callables:
    ``http_client``, ``Timeout``, ``OpenAI``) or starts with one of its modules
    (``httpx.AsyncClient``).
    """
    if change.kind == DEPENDENCY_SWITCHED:
        d = change.dependency or {}
        modules = {str(m).lower() for m in (*d.get("old_modules", ()), *d.get("new_modules", ()))}
        return segments[-1] in _dependency_terms(change) or segments[0] in modules
    name = change.name.lower()
    owner = (change.owner or "").lower()
    parameter = (change.parameter or "").lower()
    last = segments[-1]
    if change.kind in (REMOVED, MOVED) and name in segments[:-1]:
        return True
    if len(segments) == 1 or not strict:
        return last in (name, owner, parameter)
    before = segments[-2]
    if last == owner:
        return True
    if last == parameter:
        return before == name
    if last != name:
        return False
    if owner:
        return before == owner
    return before in change.path.lower().split(".")[:-1]


def _dependency_terms(change: APIChange) -> set[str]:
    """What names a switched dependency (lower case): see :func:`_names`."""
    d = change.dependency or {}
    terms = {change.name, change.switched_to or ""}
    terms |= {str(m) for m in (*d.get("old_modules", ()), *d.get("new_modules", ()))}
    terms |= {str(n) for n in (*d.get("names", ()), *d.get("parameters", ()))}
    terms |= {p.rsplit(".", 1)[-1] for p in change.import_paths or ()}
    return {t.lower() for t in terms if t}


_WORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z0-9]+|[A-Z]")


def _words(segment: str) -> list[str]:
    """``"MessageStream"`` -> ``["message", "stream"]``; ``"hf_hub_download"`` -> 3 words."""
    return [w.lower() for w in _WORD.findall(segment)]


def _in_segment(needle: str, segment: str) -> bool:
    """Whether ``needle`` is a whole segment or a run of whole words in it.

    ``legacy`` is in ``legacy_fetch`` and ``hub_download`` in ``hf_hub_download``, but
    ``messages`` is not in ``MessageStream``.
    """
    if needle == segment.lower():
        return True
    want, have = _words(needle), _words(segment)
    return bool(want) and any(have[i : i + len(want)] == want for i in range(len(have)))


def _without_root(path: str) -> str:
    """``"anthropic.resources.Messages.create"`` -> ``"resources.Messages.create"``."""
    return path.split(".", 1)[1] if "." in path else path


def _contains(change: APIChange, needle: str, *, whole: bool = False) -> bool:
    """Whether a symbol written as text appears in one of the change's paths.

    The import name is left out, so a symbol such as ``Anthropic`` or ``hub`` does not match
    every change of its package. A dotted symbol, or one that is ``whole`` (a class name such
    as ``Redis``, which is not ``RedisCluster``), must appear as whole segments.
    """
    fields = [change.path, change.short_path, change.moved_to, *change.also]
    fields += change.import_paths or ()  # re-exports: ``huggingface_hub.hf_hub_download``
    parts = [_without_root(f).split(".") for f in fields if f]
    if change.parameter:
        parts.append([change.parameter])
    want = needle.split(".")
    if len(want) == 1:
        if whole:
            return any(seg.lower() == needle for segs in parts for seg in segs)
        return any(_in_segment(needle, seg) for segs in parts for seg in segs)
    return any(
        [s.lower() for s in segs[i : i + len(want)]] == want
        for segs in parts
        for i in range(len(segs) - len(want) + 1)
    )


def _names_package(symbol: str, package: str, changes: list[APIChange]) -> bool:
    """Whether ``symbol`` is just the package or its import name (``"openai"``), spelled as
    they are: ``Redis`` is redis's client class, ``Anthropic`` anthropic's."""
    text = symbol.strip().strip("`")
    roots = {c.path.split(".", 1)[0] for c in changes}
    return text in roots or (
        text == text.lower() and canonicalize_name(text) == canonicalize_name(package)
    )


def _matching(changes: list[APIChange], symbol: str) -> tuple[list[APIChange], bool]:
    """The changes a ``symbol`` names (see api_changes), and whether the match is loose.

    A change matches when the symbol appears in one of its paths, or when the symbol's last
    one or two segments name it. If nothing matches that way, the last segment alone is
    tried, since the segment before it may be a variable (``c.create``); that looser match is
    flagged so the caller can say so.
    """
    terms = _symbol_terms(symbol)
    needles = [".".join(t) for t in terms]
    # A class name as written (``Redis``, not ``LEGACY`` or ``legacy``) names whole segments.
    whole = [
        len(t) == 1 and re.match(r"[A-Z][a-z0-9]", t[0]) is not None
        for t in _symbol_terms(symbol, keep_case=True)
    ]
    found = [
        c
        for c in changes
        if any(_contains(c, n, whole=w) for n, w in zip(needles, whole, strict=True))
        or any(_names(c, t, strict=True) for t in terms)
    ]
    if found or all(len(t) == 1 for t in terms):
        return found, False
    return [c for c in changes if any(_names(c, t, strict=False) for t in terms)], True


def _no_match(symbol: str, old: Release, new: Release, total: int) -> str:
    terms = _symbol_terms(symbol)
    bare = symbol.replace("`", "").split("(")[0].split(",")[0].strip().split(".")[-1]
    retry = (
        f'retry with the bare function or class name (symbol="{bare}")'
        if terms and len(terms[0]) > 1 and bare
        else "check the spelling, or retry with the class name instead of the method"
    )
    return (
        f'Nothing in this diff matches "{symbol}": either it did not change between '
        f"{old.version} and {new.version}, or the diff names it differently. To make sure, "
        f"{retry}, or leave `symbol` out to see all {_plural(total, 'change')}."
    )


def _split_requirement(package: str) -> tuple[str, str | None]:
    """``"openai==1.2.0"`` -> ``("openai", "1.2.0")``; extras and markers are ignored."""
    text = package.strip()
    if not text:
        raise SinceCutoffError("`package` is empty: pass a PyPI name such as 'openai'")
    try:
        req = Requirement(text)
    except InvalidRequirement:
        raise SinceCutoffError(f"'{text}' is not a PyPI package name") from None
    pins = [s.version for s in req.specifier if s.operator in ("==", "===")]
    return canonicalize_name(req.name), (pins[0] if len(pins) == 1 else None)


def _project_root(project_dir: str) -> Path:
    path = Path(project_dir or ".").expanduser()
    if path.is_absolute():
        return path
    # Claude Code sets CLAUDE_PROJECT_DIR for the servers it starts; others start them in it.
    base = os.environ.get("CLAUDE_PROJECT_DIR")
    return (Path(base) if base else Path.cwd()) / path


def _plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n} {word if n == 1 else many or word + 's'}"


def _day(release: Release) -> str:
    return release.uploaded.date().isoformat()


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


# --------------------------------------------------------------------- server
class _Progress(Reporter):
    """The engine's progress as MCP progress notifications.

    The value counts steps (a stage starting, a dependency looked up, a package diffed) and
    only ever grows, as the protocol requires. Until the last stage starts, the total is an
    upper bound: project_changes first assumes every dependency needs a diff, then learns how
    many do.
    """

    def __init__(self, send: ProgressFn, *, stages: int = 1) -> None:
        self.send = send
        self.stages_left = stages
        self.step = 0
        self.total = 0
        self.title = ""
        self.stage_total = 0
        self.stage_done = 0

    def stage(self, title: str, total: int | None = None) -> None:
        n = total or 0
        self.stages_left -= 1
        self.step += 1
        # This stage's steps, and at most one start and n steps for each stage after it.
        self.total = self.step + n + max(0, self.stages_left) * (n + 1)
        self.title, self.stage_total, self.stage_done = title, n, 0
        self._send()

    def advance(self, n: int = 1, *, label: str | None = None) -> None:
        self.step += n
        self.stage_done += n
        self._send()

    def _send(self) -> None:
        count = f" ({self.stage_done} of {self.stage_total})" if self.stage_done else ""
        try:
            self.send(self.step, max(self.total, self.step), self.title + count)
        except Exception as exc:  # progress is a courtesy: never fail the call over it
            log.debug("progress notification failed: %s", exc)


def _arg_docs(doc: str) -> dict[str, str]:
    """The ``Args:`` entries of a Google-style docstring, one line of text per parameter."""
    lines = inspect.cleandoc(doc).splitlines()
    out: dict[str, list[str]] = {}
    current: list[str] | None = None
    inside = False
    for line in lines:
        if line.strip() == "Args:":
            inside = True
            continue
        if not inside:
            continue
        if not line.strip() or not line.startswith(" "):
            break
        entry = re.match(r"^    (\w+): (.*)$", line)
        if entry:
            current = out.setdefault(entry.group(1), [entry.group(2)])
        elif current is not None:
            current.append(line.strip())
    return {name: " ".join(words) for name, words in out.items()}


def _description(doc: str) -> str:
    """The tool description: the docstring without its ``Args:`` block.

    The parameters are described in the input schema instead, one description each.
    """
    text = inspect.cleandoc(doc)
    return re.sub(r"\nArgs:\n(?:    .*\n|\n(?=    ))*", "", text + "\n").strip()


def _tool_function(fn: Callable[..., str]) -> Callable[..., str]:
    """``fn`` the way the SDK should see it.

    Its parameters carry their docstring descriptions (the input schema shows them), known
    failures become tool errors with their message instead of server crashes, and a
    ``progress`` parameter is replaced by the SDK's request context, through which progress
    notifications are sent.
    """
    import anyio.from_thread
    from mcp.server.mcpserver import Context
    from mcp.server.mcpserver.exceptions import ToolError
    from pydantic import Field

    docs = _arg_docs(fn.__doc__ or "")
    signature = inspect.signature(fn, eval_str=True)
    params = []
    wants_progress = False
    for param in signature.parameters.values():
        if param.name == "progress":
            wants_progress = True
            continue
        annotation = param.annotation
        if param.name in docs:
            annotation = Annotated[annotation, Field(description=docs[param.name])]
        params.append(param.replace(annotation=annotation))
    if wants_progress:
        params.append(inspect.Parameter("ctx", inspect.Parameter.KEYWORD_ONLY, annotation=Context))

    def call(*args: Any, **kwargs: Any) -> str:
        ctx = kwargs.pop("ctx", None)
        if ctx is not None:

            def progress(done: float, total: float | None, message: str) -> None:
                # Tools run in a worker thread; the notification is sent from the event loop.
                anyio.from_thread.run(ctx.report_progress, done, total, message)

            kwargs["progress"] = progress
        try:
            return fn(*args, **kwargs)
        except (SinceCutoffError, ValueError, OSError) as exc:
            raise ToolError(str(exc)) from exc

    call.__name__ = fn.__name__
    call.__qualname__ = fn.__qualname__
    call.__doc__ = fn.__doc__
    call.__signature__ = signature.replace(parameters=params)  # type: ignore[attr-defined]
    call.__annotations__ = {p.name: p.annotation for p in params} | {"return": str}
    return call


def build_server(
    tools: Tools | None = None, *, log_level: Literal["DEBUG", "WARNING"] = "WARNING"
) -> MCPServer[Any]:
    """The MCP server with the three read-only tools registered.

    Building it and listing its tools needs no network access and no API key.
    """
    try:
        from mcp.server.mcpserver import MCPServer
        from mcp.types import ToolAnnotations
    except ImportError as exc:
        raise SinceCutoffError(
            f"the MCP server needs the 'mcp' package ({exc}); reinstall since-cutoff"
        ) from exc

    tools = tools or Tools()
    server: MCPServer[Any] = MCPServer(
        name="since-cutoff",
        title="since-cutoff",
        description="What changed in your Python dependencies since the model's training cutoff.",
        instructions=INSTRUCTIONS,
        website_url="https://github.com/MohammadHijjawi97/since-cutoff",
        version=__version__,
        log_level=log_level,
    )
    entries: list[tuple[Callable[..., str], str]] = [
        (tools.model_cutoff, "Model training cutoff"),
        (tools.api_changes, "Library API changes since the cutoff"),
        (tools.project_changes, "Project dependency changes since the cutoff"),
    ]
    for fn, title in entries:
        server.add_tool(
            _tool_function(fn),
            title=title,
            description=_description(fn.__doc__ or ""),
            annotations=ToolAnnotations(
                title=title,
                read_only_hint=True,
                destructive_hint=False,
                idempotent_hint=True,
                open_world_hint=True,
            ),
            structured_output=False,
        )

    @server.prompt(
        name="check_project",
        title="Check a project's dependency changes",
        description="Check a project against the coding agent's own training cutoff.",
    )
    def check_project(project_dir: str = ".") -> str:
        return (
            f'Call project_changes(project_dir="{project_dir}", model=<your own model id>). '
            "Start with changed APIs the project uses in the old form, and keep the tool's "
            "evidence wording: removals, moves and parameter changes come from a static API diff; "
            "similar names are not confirmed replacements. Then suggest since-cutoff sync "
            "if the user wants to keep the resulting notes in AGENTS.md."
        )

    @server.prompt(
        name="before_upgrade",
        title="Check changes before a dependency upgrade",
        description="Check what project code may need to change before upgrading one package.",
    )
    def before_upgrade(package: str, to_version: str = "") -> str:
        target = (
            f', to_version="{to_version}"'
            if to_version
            else " (omit to_version to compare with the latest release)"
        )
        return (
            f"Read the pinned version of {package!r} from the project's lockfile. "
            f'Call api_changes(package="{package}", from_version=<pinned version>{target}, '
            "model=<your own model id>). List what the project's code must change, preserving "
            "the tool's evidence wording: removals, moves and parameter changes come from a "
            "static API diff; similar names are not confirmed replacements."
        )

    return server


def serve(*, max_download_mb: float = 80.0, debug: bool = False) -> None:
    """Run the MCP server on stdin/stdout until the client disconnects."""
    logging.getLogger("griffe").setLevel(logging.CRITICAL + 1)
    # Diffs of several packages run in worker processes. The server is multi-threaded, where
    # fork() can deadlock, so start workers fresh (already the default on Windows and macOS).
    multiprocessing.set_start_method("spawn", force=True)
    tools = Tools(max_download_mb=max_download_mb, processes=True)
    build_server(tools, log_level="DEBUG" if debug else "WARNING").run("stdio")
