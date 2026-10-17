"""``since-cutoff sync`` and ``since-cutoff status``: keep the notes block in AGENTS.md /
CLAUDE.md in step with the lockfile and with the code.

``sync`` scans the project as ``scan`` does (PyPI's release lists, and the API diffs, cached per
package and version pair, so only a bumped package is diffed again) and writes the notes from
the API diff for the changed APIs the code uses (:func:`propose`). Every byte outside the
since-cutoff markers stays as it is. It uses the model and cutoff the block was written for,
unless ``--model`` or ``--cutoff`` says otherwise, so that teammates whose agents use other
models do not rewrite the block back and forth. A ``[type-checked]`` note that ``since-cutoff
run`` wrote stays while its package's version is the same.

``status`` needs no network (:func:`target_status`): it compares the versions the block's notes
are for, and its hash of the dependencies, with the lockfile. It fails (exit code 3) only where
``sync --check`` would, as far as that can be told offline: it does not read the code for new
uses of changed APIs (``sync --check`` does), and no block is no failure. A coding agent set up
with a model whose cutoff is earlier than the block's is said, with the ``sync --model`` command
for it, and is not a failure either: sync keeps the block's model.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from since_cutoff import __version__
from since_cutoff.apidiff import APIChange
from since_cutoff.engine import (
    KNOWN,
    NEW,
    SKIPPED,
    UNCHANGED,
    ModelTarget,
    PackageScan,
    ScanResult,
    dependency_pairs,
    scanned_dependencies,
)
from since_cutoff.models import DEFAULT_CUTOFF_MARGIN
from since_cutoff.notes import (
    BLOCK_END,
    BLOCK_VERSION,
    IMPORTED_APIS,
    NOTE_MODEL,
    SCOPE_FAILURES,
    SCOPE_IMPORTED,
    SCOPE_USED,
    TAG_TYPE_CHECKED,
    Note,
    NotesFileError,
    ParsedBlock,
    agents_import_tip,
    api_id,
    apply_block,
    block_targets,
    block_text,
    deps_hash,
    has_markers,
    model_names,
    parse_block,
    read_text,
    remove_block,
    render_block,
    tag_text,
    with_block,
    without_block,
)
from since_cutoff.project import VENV_DIRS, Project
from since_cutoff.rules import (
    FRONT_KEY,
    RuleFormat,
    check_ours,
    delete,
    foreign,
    format_of,
    front_edited,
    front_hash,
    ignored,
    present,
    rule_format,
    split_front,
    with_rule,
    without_rule,
    write_rule,
)

# Exit codes of sync and status (0 and 1 as everywhere; 3 is the CLI's "found something").
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_OUT_OF_DATE = 3
EXIT_EDITED = 4  # sync: the block was edited by hand, and --force was not given

# ``sync --scope``: what the notes cover (the block's meta line says it too).
SCOPES = (SCOPE_USED, SCOPE_IMPORTED)
SCOPE_TEXT = {
    SCOPE_USED: "the changed APIs your code uses",
    SCOPE_IMPORTED: "the changes most likely to matter in the packages your code imports",
    SCOPE_FAILURES: "what `since-cutoff run` found the model getting wrong",
}

# ``status``: the state of a block (TargetStatus.state) and of a package's notes in it.
CURRENT = "current"
OUT_OF_DATE = "out_of_date"
NEVER_RUN = "never_run"
ORPHAN = "orphan"  # notes for a package that is no longer a dependency
UNPINNED = "unpinned"  # nothing pins its version, so status cannot compare it offline
STATE_TEXT = {
    CURRENT: "current",
    OUT_OF_DATE: "out of date",
    ORPHAN: "no longer a dependency",
    UNPINNED: "not pinned (sync checks it)",
}
# Why a block is out of date (TargetStatus.problems).
STALE_VERSION = "stale"
ORPHANED = "orphan"
DEPENDENCIES_CHANGED = "dependencies_changed"
VERSIONS_FROM_CHANGED = "versions_from_changed"
# The block compared from another day than the scan does now (--cutoff-margin): a block
# written before the margin existed compared from the cutoff itself.
MARGIN_CHANGED = "margin_changed"
OLD_FORMAT = "old_format"  # written by since-cutoff 0.3: sync upgrades it
RUN_BLOCK = "run_block"  # written by `run --apply`: sync writes the notes for the used APIs
# Not a reason the block is out of date (sync keeps the block's model): a coding agent set up
# with a model whose cutoff is earlier than the block's, which the notes may not cover.
MODEL_CHANGED = "model_changed"

SYNC = "`since-cutoff sync`"


# ------------------------------------------------------------ the target files
@dataclass
class TargetFile:
    """An instructions file that holds, or will hold, the notes block (notes.block_targets)."""

    path: Path
    name: str  # as the output names it: relative to the project, with "/"
    text: str | None  # None: there is no such file yet
    block: ParsedBlock | None
    written: str | None  # the block as written, with LF line breaks
    # A path-scoped rule file (since_cutoff.rules): its format; None for AGENTS.md and the like.
    rule: RuleFormat | None = None

    @property
    def package(self) -> str | None:
        """The package a rule file is for (from its name); None for any other file."""
        return self.rule.package_of(self.path) if self.rule is not None else None

    @property
    def meta(self) -> dict[str, Any]:
        return self.block.meta if self.block is not None else {}

    @property
    def basis(self) -> tuple[str, date] | None:
        """The model (``""`` for a cutoff alone; several joined with ", ") and the cutoff the
        block was written for, or None when there is no block that says."""
        cutoff = self.meta.get("cutoff")
        try:
            day = date.fromisoformat(cutoff) if isinstance(cutoff, str) else None
        except ValueError:
            day = None
        if day is None:
            return None
        model = self.meta.get("model")
        return (model if isinstance(model, str) else ""), day

    @property
    def scope(self) -> str | None:
        """The block's scope, as ``sync`` keeps it: the notes of a ``run`` (failures) become
        those of the changed APIs the code uses."""
        if self.block is None:
            return None
        scope = self.meta.get("scope")
        return scope if scope in SCOPES else SCOPE_USED

    @property
    def edited(self) -> bool:
        """Edited by hand since since-cutoff wrote it (a 0.3 block has no hash to tell); for a
        rule file, its front matter too (the meta line's ``front`` hash)."""
        if self.block is None:
            return False
        if self.block.edited is True:
            return True
        return (
            self.rule is not None and self.text is not None and front_edited(self.text, self.meta)
        )


def read_target(root: Path, path: Path) -> TargetFile:
    """The file and its block (NotesFileError for markers that cannot be read). A rule file's
    path (since_cutoff.rules) that holds a file since-cutoff did not write is refused."""
    try:
        name = path.relative_to(root).as_posix()
    except ValueError:
        name = str(path)
    rule = format_of(root, path)
    if rule is not None:
        check_ours(root, path)
    text = read_text(path)
    if text is None:
        return TargetFile(path, name, None, None, None, rule)
    block = parse_block(text, path.name)
    why = foreign(rule, path, text) if rule is not None else None
    if why:  # another package's block: nothing of it is written or removed
        raise NotesFileError(why)
    return TargetFile(path, name, text, block, block_text(text, path.name), rule)


@dataclass
class RuleTargets:
    """The path-scoped rule files of one format (``sync --target cursor-rules``): those
    since-cutoff wrote in the project, one per package. ``sync`` writes one for each package
    with notes and removes the others (:func:`propose_rules`)."""

    rule: RuleFormat
    root: Path
    files: list[TargetFile]

    @property
    def placeholder(self) -> TargetFile:
        """The folder, standing for files there are none of yet."""
        return TargetFile(
            self.root / self.rule.folder, self.rule.folder, None, None, None, self.rule
        )

    @property
    def first(self) -> TargetFile:
        """The file whose block says the model, cutoff and scope the others share (they are
        written together), else :attr:`placeholder`."""
        return next((t for t in self.files if t.block is not None), self.placeholder)

    @property
    def name(self) -> str:
        return self.first.name

    @property
    def basis(self) -> tuple[str, date] | None:
        return self.first.basis

    @property
    def scope(self) -> str | None:
        return self.first.scope

    @property
    def meta(self) -> dict[str, Any]:
        return self.first.meta


Target = TargetFile | RuleTargets


def read_rules(root: Path, rule: RuleFormat) -> RuleTargets:
    return RuleTargets(rule, root, [read_target(root, p) for p in rule.files(root)])


def read_targets(root: Path, explicit: str | None = None) -> list[Target]:
    """What ``sync``, ``status`` and ``explain`` work on: ``--target``, a file or a rule
    format (``cursor-rules``); otherwise the files of notes.block_targets and the rule
    formats that have files since-cutoff wrote (rules.present). When only rule files have
    notes, AGENTS.md and CLAUDE.md are left alone: the notes stay where they were written."""
    rule = rule_format(explicit)
    if rule is not None:
        return [read_rules(root, rule)]
    formats = [] if explicit else present(root)
    files = block_targets(root, explicit)
    if formats and not any(has_markers(p) for p in files):
        files = []
    found: list[Target] = [read_target(root, p) for p in files]
    return found + [read_rules(root, f) for f in formats]


def target_files(target: Target) -> list[TargetFile]:
    """The files of a target: itself, or a format's rule files (none yet: its folder)."""
    if isinstance(target, RuleTargets):
        return target.files or [target.placeholder]
    return [target]


def sticky_target(target: Target, margin: int = DEFAULT_CUTOFF_MARGIN) -> ModelTarget | None:
    """The model and cutoff of the block in ``target``, as sync keeps them, or None. The margin
    is not the block's: it is the scan's setting (``--cutoff-margin``), the same for everyone,
    and ``status`` says when a block was compared from another day."""
    basis = target.basis
    if basis is None:
        return None
    model, cutoff = basis
    how = "--model" if model else "--cutoff"
    source = f"the notes in {target.name}; {how} to change"
    if not model:
        return ModelTarget.cutoff_only(cutoff, source, margin=margin)
    return ModelTarget(model, model, cutoff, source, margin=margin)


def block_margin(meta: dict[str, Any]) -> int:
    """How many days before the cutoff a block's comparison releases were published by (its
    meta line's ``margin``); 0 for a block written before there was a margin."""
    margin = meta.get("margin")
    return margin if isinstance(margin, int) and margin >= 0 else 0


def margin_text(margin: int) -> str:
    """``30 days before the cutoff``, or ``the cutoff itself``."""
    return f"{margin} days before the cutoff" if margin else "the cutoff itself"


# ------------------------------------------------------------------ sync
@dataclass
class Change:
    """One reason why a block changes: what sync does (``done``) and how ``--check`` says it
    is out of date (``state``). ``package`` is None for the block as a whole."""

    package: str | None
    done: str
    state: str


@dataclass
class Proposal:
    """What sync would write into one file, and why."""

    target: TargetFile
    model: str
    cutoff: date
    scope: str
    versions_from: str
    block: str | None  # the block sync writes; None: the file has none after sync
    new_text: str | None  # the file after sync; None: it still does not exist
    # "created", "appended to", "updated", "removed from"; for a rule file also "deleted" (the
    # file goes) and "emptied" (its notes go, the user's text in it stays); None: nothing.
    action: str | None
    notes: int
    # Days before the cutoff the comparison releases were published by (--cutoff-margin).
    margin: int = DEFAULT_CUTOFF_MARGIN
    # SCOPE_IMPORTED: APIs noted per package (``--per-package``, or the block's own choice).
    per_package: int = IMPORTED_APIS
    changes: list[Change] = field(default_factory=list)
    # Packages whose [type-checked] notes were dropped because their version changed, with
    # the version they were for (`since-cutoff run --only <pkg>` tests the model again).
    retest: dict[str, str] = field(default_factory=dict)
    # The APIs each bullet of the new block names (Note.apis; several for a bullet that says
    # the same of several), by its line: a bullet whose API had one before is "changed", not
    # "added" and "dropped".
    apis: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        return self.new_text != self.target.text

    @property
    def edited(self) -> bool:
        return self.target.edited

    @property
    def basis_meta(self) -> dict[str, Any]:
        """The model, cutoff, margin and scope the proposal is for, as a meta line names them
        (for rules.again_command)."""
        return {
            "model": self.model,
            "cutoff": self.cutoff.isoformat(),
            "margin": self.margin,
            "scope": self.scope,
        }


def propose(
    scan: ScanResult,
    target: TargetFile,
    *,
    scope: str = SCOPE_USED,
    suggestions: bool = False,
    per_package: int | None = None,
) -> Proposal:
    """The block sync writes into ``target`` for ``scan`` (its model and cutoff), the file's
    text after that, and why it changes. For a rule file (TargetFile.rule), only the notes for
    its package, under the front matter that names the files they apply to
    (:func:`rule_paths`); a rule file left with no notes goes (action "deleted").

    The notes are :meth:`ScanResult.scope_notes` (``per_package``: how many APIs a package
    gets with SCOPE_IMPORTED; :data:`IMPORTED_APIS` by default). The ``[type-checked]`` notes of the block in
    the file stay, in place of the notes from the diff for their APIs, while their package
    keeps the same version, the code still uses their API and the model and cutoff are the
    same (:func:`_kept_notes`); the notes from the diff are then built again without those
    APIs, so that a bullet one of them shared with other APIs still names the others. A
    section goes when its package is no longer a dependency, is no newer than the release at
    the cutoff, or has no changed API the code uses. When the notes are the same as those in
    the file, the block stays exactly as it is, even if another version of since-cutoff wrote
    it.
    """
    model, cutoff = scan.target.model_id, scan.target.cutoff
    same = target.basis == (model, cutoff)
    old = target.block
    per_package = per_package or IMPORTED_APIS
    rule, package = target.rule, target.package

    def scoped(exclude: set[str] | None = None) -> list[Note]:
        found = scan.scope_notes(
            scope, suggestions=suggestions, per_package=per_package, exclude=exclude or set()
        )
        return found if rule is None else [n for n in found if n.change.package == package]

    notes = scoped()
    kept: list[Note] = []
    retest: dict[str, str] = {}
    replaced: set[str] = set()
    if same and old is not None:
        kept, retest, replaced = _kept_notes(old, notes)
    if replaced:
        # A [type-checked] note replaces the note from the diff for its API: the others' notes
        # again, without it (a merged bullet names only the APIs left to it).
        notes = scoped(replaced)
    notes += kept
    versions_from = scan.project.version_source
    block: str | None = None
    new_text: str | None
    action: str | None
    options: dict[str, Any] = {
        "model": model,
        "cutoff": cutoff,
        "version_source": versions_from,
        "deps": scan.deps_hash(),
        "scope": scope,
        "suggestions": suggestions,
        "per_package": per_package,
        "margin": scan.target.margin,
    }
    if notes:
        front = ""
        if rule is not None and package is not None:
            paths = rule_paths(scan, package, scope)
            front = rule.front_matter(paths, package, notes[0].change.to_version)
            options["extra"] = {FRONT_KEY: front_hash(front)}
        block = render_block(notes, **options)
        tool = target.meta.get("tool")
        if old is not None and old.version >= BLOCK_VERSION and isinstance(tool, str):
            earlier = render_block(notes, **options, tool=tool)
            if earlier == target.written:
                block = earlier  # the same notes: as that version of since-cutoff wrote them
        if rule is not None:
            new_text, action = with_rule(target.text, front, block, target.path.name)
        else:
            new_text, action = with_block(target.text, block, target.name)
    elif target.text is not None and old is not None and rule is not None:
        if without_rule(target.text, target.path.name) is None:
            new_text, action = None, "deleted"
        else:
            # The user's own text stays, under the front matter it had, after a block with
            # no notes (only the markers and the meta line): the file stays since-cutoff's,
            # so that the next sync that has notes for the package writes them there again.
            front, _ = split_front(target.text)
            options["extra"] = {FRONT_KEY: front_hash(front)}
            empty = render_block([], **options, empty=True)
            tool = target.meta.get("tool")
            if not old.packages and isinstance(tool, str):
                earlier = render_block([], **options, empty=True, tool=tool)
                if earlier == target.written:
                    empty = earlier  # as that version of since-cutoff wrote it
            new_text, _ = with_rule(target.text, front, empty, target.path.name)
            action = "emptied" if old.packages else "updated"
    elif target.text is not None and old is not None:
        new_text, action = without_block(target.text, target.name), "removed from"
    else:
        new_text, action = target.text, None
    proposal = Proposal(
        target, model, cutoff, scope, versions_from, block, new_text, action, len(notes)
    )
    proposal.margin = scan.target.margin
    proposal.per_package = per_package
    proposal.retest = retest
    proposal.apis = {n.line: tuple(n.apis) for n in notes}
    if not proposal.changed:
        proposal.action = None
    else:
        proposal.changes = _changes(scan, target, proposal, same=same)
    return proposal


def rule_paths(scan: ScanResult, package: str, scope: str = SCOPE_USED) -> list[str]:
    """The files a rule file for ``package`` applies to (relative to the project, with "/",
    sorted): those that use its changed APIs (the scan's locations, UsedAPI.files); for
    SCOPE_IMPORTED, whose notes are also about APIs the code does not use, and for a package
    first released after the cutoff, those that import it (:func:`importing_files`). Files git
    ignores are left out (rules.ignored): a committed rule file names no local file nobody
    else has. [] when no file is known: the rule then applies everywhere
    (rules.RuleFormat.front_matter, which makes the globs: rules.globs_for)."""
    p = next((s for s in scan.packages if s.name == package), None)
    if p is None:
        return []
    skip = _ignored_files(scan)
    if scope != SCOPE_IMPORTED:
        used = {f for u in scan.used_apis() if u.package.name == package for f in u.files}
        if used - skip:
            return sorted(used - skip)
    return sorted(set(importing_files(scan, p)) - skip)


def importing_files(scan: ScanResult, p: PackageScan) -> list[str]:
    """The files of the project's code that import the package (ScanResult.uses); for one
    first released after the cutoff, whose import names the scan did not read, those that
    import a module that names it (``foo_bar`` for foo-bar)."""
    if p.import_names:
        files = scan.uses(p)
    else:
        modules = [m for m in scan.project.imported_modules if canonicalize_name(m) == p.name]
        files = scan.project.code_use(modules) if modules else ()
    return sorted({f.file for f in files if f.file})


def _ignored_files(scan: ScanResult) -> set[str]:
    """The files of the project's code that git ignores (rules.ignored), asked once a scan."""
    cached = scan.__dict__.get("_ignored")
    if cached is None:
        cached = ignored(scan.project.root, sorted({f.file for f in scan.project.files if f.file}))
        scan.__dict__["_ignored"] = cached
    return set(cached)


def propose_rules(
    scan: ScanResult,
    targets: RuleTargets,
    *,
    scope: str = SCOPE_USED,
    suggestions: bool = False,
    per_package: int | None = None,
) -> list[Proposal]:
    """What sync writes into a format's rule files (:func:`propose` for each): the files
    there now, each for its package (one whose package has no notes left goes), then a new
    file for each package with notes that has none, in the order of the packages' names.
    With no file and no notes, one proposal for the folder that writes nothing (sync says
    why)."""
    out = [
        propose(scan, t, scope=scope, suggestions=suggestions, per_package=per_package)
        for t in targets.files
    ]
    have = {t.package for t in targets.files}
    notes = scan.scope_notes(scope, suggestions=suggestions, per_package=per_package or None)
    for package in sorted({n.change.package for n in notes} - have):
        t = read_target(targets.root, targets.rule.path(targets.root, package))
        out.append(propose(scan, t, scope=scope, suggestions=suggestions, per_package=per_package))
    if not out:
        out.append(
            propose(
                scan,
                targets.placeholder,
                scope=scope,
                suggestions=suggestions,
                per_package=per_package,
            )
        )
    return out


def write_proposal(p: Proposal) -> None:
    """Write what sync proposes into its file: the file's new text, or, for a rule file left
    with nothing of its own, no file (NotesFileError when that fails)."""
    if p.target.rule is not None:
        if p.new_text is None:
            delete(p.target.path)
        else:
            write_rule(p.target.path, p.new_text)
    elif p.block is None:
        remove_block(p.target.path)
    else:
        apply_block(p.target.path, p.block)


def _kept_notes(old: ParsedBlock, notes: list[Note]) -> tuple[list[Note], dict[str, str], set[str]]:
    """The ``[type-checked]`` notes of the block in the file that stay, for a package whose
    version is the same: each while the code still uses its API (the meta line's ``checked``
    says which API each is about: notes.api_id), in place of the note from the diff for that
    API. A block that does not say (edited, or written before it did) keeps them per package:
    while the package has a section. Also, for a package whose version changed, the version
    its ``[type-checked]`` notes were for; and the APIs (APIChange.api_key) whose note from
    the diff they replace.
    """
    # Every API a note names (Note.covered: a merged bullet names several), with its change.
    by_api: dict[str, tuple[Note, APIChange]] = {}
    for n in notes:
        for c in n.covered:
            by_api.setdefault(api_id(c), (n, c))
    first: dict[str, Note] = {}
    for n in notes:
        first.setdefault(n.change.package, n)
    checked = [
        (package, section, text, tags)
        for package, section in old.packages.items()
        for text, tags in section.bullets
        if TAG_TYPE_CHECKED in tags
    ]
    ids = old.meta.get("checked")
    known = (
        not old.edited
        and isinstance(ids, list)
        and len(ids) == len(checked)
        and all(isinstance(i, str) for i in ids)
    )
    kept: list[Note] = []
    retest: dict[str, str] = {}
    replaced: set[str] = set()
    for i, (package, section, text, tags) in enumerate(checked):
        if known and ids:
            change = by_api.get(ids[i], (None, None))[1]
        else:
            note = first.get(package)
            change = note.change if note is not None else None
        if change is None or change.package != package:
            continue  # its API is no longer used, or the whole section goes (Change says why)
        if not _same_version(change.to_version, section.version):
            retest[package] = section.version
            continue
        # The package, versions and API are the change's; the text and tags are the bullet's.
        kept.append(Note(change, text, None, True, NOTE_MODEL, tags))
        if known:
            replaced.add(change.api_key)
    return kept, retest, replaced


def _changes(scan: ScanResult, target: TargetFile, p: Proposal, *, same: bool) -> list[Change]:
    old = target.block
    new = parse_block(p.block, target.name) if p.block else None
    out: list[Change] = []
    if old is None:
        return out
    if new is None and not old.packages:  # a rule file's block with no notes, kept for its text
        if p.new_text is None:  # which is gone now: so is the file
            return [
                Change(
                    None,
                    "no notes and no text of your own left",
                    "it has no notes and no text of your own left, so the file would go",
                )
            ]
        return [Change(None, "its metadata written again", "its metadata would change")]
    if target.edited:
        out.append(Change(None, "replaced the text edited by hand", "the block was edited by hand"))
    if not same and target.basis is not None:
        before, after = basis_text(*target.basis), basis_text(p.model, p.cutoff)
        out.append(
            Change(
                None,
                f"written for {after} (it was for {before})",
                f"it is for {before}, not {after}",
            )
        )
    if old.version < BLOCK_VERSION:
        out.append(
            Change(
                None,
                "upgraded from since-cutoff 0.3's format",
                "it is in since-cutoff 0.3's format",
            )
        )
    elif old.meta.get("scope") != p.scope and new is not None:
        was_scope = SCOPE_TEXT.get(str(old.meta.get("scope")), str(old.meta.get("scope")))
        out.append(
            Change(
                None,
                f"now for {SCOPE_TEXT[p.scope]} (it was for {was_scope})",
                f"it is for {was_scope}, not {SCOPE_TEXT[p.scope]}",
            )
        )
    by_name = {s.name: s for s in scan.packages}
    old_packages = old.packages
    new_packages = new.packages if new is not None else {}
    for name in sorted(set(old_packages) | set(new_packages)):
        was, now = old_packages.get(name), new_packages.get(name)
        if was is None:
            assert now is not None
            count = _plural(len(now.bullets), "note")
            out.append(
                Change(
                    name,
                    f"{name} {now.version}: {count} added",
                    f"{name} {now.version} has {count} to add",
                )
            )
            continue
        if now is None:
            done, state = _dropped(scan, by_name.get(name), name, p.scope)
            out.append(Change(name, f"{name} dropped ({done})", state))
            continue
        old_lines = [_line(b) for b in was.bullets]
        new_lines = [_line(b) for b in now.bullets]
        added = [line for line in new_lines if line not in old_lines]
        gone = [line for line in old_lines if line not in new_lines]
        what = _counts(added, gone, p.apis, name)
        retest = p.retest.get(name)
        again = (
            f"; its [type-checked] notes were for {retest}, so the notes from the diff take "
            f"their place (`since-cutoff run --only {name}` tests the model again)"
            if retest
            else ""
        )
        if not _same_version(was.version, now.version):
            where = scan.versions_from(by_name[name]) if name in by_name else p.versions_from
            # After a hand edit, the text sync replaces is not the one it wrote: no counts.
            counted = "" if target.edited else f", {what}"
            out.append(
                Change(
                    name,
                    f"{name} checked again for {now.version} "
                    f"({_plural(len(new_lines), 'note')}{counted}){again}",
                    f"{name} {was.version} in the notes, {now.version} in {where}",
                )
            )
        elif target.edited:
            continue  # "replaced the text edited by hand" says it
        elif added or gone or was.cutoff_version != now.cutoff_version:
            out.append(
                Change(
                    name,
                    f"{name} {now.version}: {what}{again}",
                    f"the notes for {name} {now.version} would change ({what})",
                )
            )
    if new is None or not same or old.version < BLOCK_VERSION:
        return out
    # The hash changed although every package with notes kept its version: another did.
    locked = {d.key: d.version for d in scan.project.dependencies}
    locked.update((s.name, s.locked) for s in scan.packages)
    moved = any(
        not locked.get(name) or not _same_version(str(locked[name]), section.version)
        for name, section in old_packages.items()
    )
    if old.meta.get("deps") != new.meta.get("deps") and not moved:
        out.append(
            Change(
                None,
                "other dependencies changed since the notes were written",
                "other dependencies changed since the notes were written",
            )
        )
    if old.meta.get("versions_from") != new.meta.get("versions_from"):
        out.append(
            Change(
                None,
                f"the versions now come from {p.versions_from}",
                f"the versions now come from {p.versions_from}, not "
                f"{old.meta.get('versions_from')}",
            )
        )
    if block_margin(old.meta) != p.margin:
        before, after = margin_text(block_margin(old.meta)), margin_text(p.margin)
        out.append(
            Change(
                None,
                f"now comparing from {after} (it was {before}; --cutoff-margin)",
                f"the notes compare from {before}, not {after} (--cutoff-margin)",
            )
        )
    if bool(old.meta.get("suggestions")) != bool(new.meta.get("suggestions")):
        what = "with" if new.meta.get("suggestions") else "without"
        out.append(
            Change(
                None,
                f"now {what} similar names (--suggestions)",
                f"the notes would be {what} similar names (--suggestions)",
            )
        )
    if p.scope == SCOPE_IMPORTED and old.meta.get("per_package") != new.meta.get("per_package"):
        out.append(
            Change(
                None,
                f"now up to {p.per_package} APIs per package (--per-package)",
                f"the notes would be for up to {p.per_package} APIs per package (--per-package)",
            )
        )
    rule = p.target.rule
    if rule is not None and p.target.text is not None and p.new_text is not None:
        # The globs the front matter names, before and after (the meta line's ``front`` hash
        # says that it changed, the front matter itself what).
        had = rule.globs_in(split_front(p.target.text)[0])
        has = rule.globs_in(split_front(p.new_text)[0])
        if had != has:
            counts = _counts(sorted(set(has) - set(had)), sorted(set(had) - set(has)))
            where = f"{_plural(len(has), 'glob')} ({counts})" if has else "every file"
            out.append(
                Change(
                    None,
                    f"now loaded for {where}",
                    f"the files it is loaded for would change ({counts})",
                )
            )
    if not out:
        out.append(
            Change(
                None,
                f"header and metadata written by since-cutoff {__version__}",
                "its header or metadata would change",
            )
        )
    return out


def _dropped(scan: ScanResult, p: PackageScan | None, name: str, scope: str) -> tuple[str, str]:
    """Why a package's section goes: (as sync says it, as ``--check`` says it)."""
    if not any(d.key == name for d in scan.project.dependencies):
        return "no longer a dependency", f"{name} is no longer a dependency"
    if p is None:
        why = "a dependency of a dependency that your code does not import"
        return f"no longer checked: {why}", f"{name} is no longer checked ({why})"
    where = scan.versions_from(p)
    if p.status == KNOWN:
        latest = f"the latest release {scan.target.compare_when}"
        if p.locked and p.cutoff_version and not _same_version(p.locked, p.cutoff_version):
            why = f"older than {p.cutoff_version}, {latest}"
        else:
            why = latest
        return f"{p.locked} is {why}", f"{name} {p.locked} in {where} is {why}"
    if p.status == UNCHANGED:
        why = f"did not change from {p.cutoff_version} to {p.locked}"
        return f"its API {why}", f"{name}'s API {why}"
    if p.status == NEW:
        if scan.target.released_within_margin(p):
            when = (
                f"first released {p.first_released}, within {scan.target.margin} days of the cutoff"
            )
        else:
            when = "first released after the cutoff"
        why = f"{when}, and your code no longer imports it"
        return why, f"{name} was {why}"
    if p.status == SKIPPED:
        return f"could not be checked: {p.reason}", f"{name} could not be checked ({p.reason})"
    if scope == SCOPE_IMPORTED and not p.imported:
        return "your code no longer imports it", f"your code no longer imports {name}"
    return (
        "your code no longer uses an API of it that changed",
        f"your code no longer uses a changed API of {name}",
    )


def blocked_by(scan: ScanResult, targets: Iterable[TargetFile]) -> list[PackageScan]:
    """The packages that could not be checked (PyPI unreachable) although a block has notes
    for them: sync cannot say whether those notes still hold, so it writes nothing."""
    noted = {name for t in targets if t.block is not None for name in t.block.packages}
    return [p for p in scan.skipped if p.name in noted]


def diff_lines(p: Proposal) -> list[str]:
    """A unified diff of the file, before and after sync (from /dev/null for a new file)."""
    before = (p.target.text or "").splitlines()
    after = (p.new_text or "").splitlines()
    source = p.target.name if p.target.text is not None else "/dev/null"
    return list(
        difflib.unified_diff(
            before, after, source, f"{p.target.name} (since-cutoff sync)", lineterm=""
        )
    )


def up_to_date_text(p: Proposal, scan: ScanResult) -> str:
    """What sync says about a file it leaves as it is."""
    name = p.target.name
    if p.block is None:
        if p.scope == SCOPE_IMPORTED:
            none = "none of the packages your code imports changed their API"
        else:
            none = "your code uses none of the APIs that changed"
        text = f"{name}: no notes to write; {none} after {after_text(p.model, p.cutoff)}."
        if p.scope == SCOPE_USED and any(s.imported for s in scan.changed):
            available = scan.scope_notes(SCOPE_IMPORTED, per_package=p.per_package)
            packages = {n.change.package for n in available}
            if available:
                text += (
                    f" `since-cutoff sync --scope imported` writes the "
                    f"{_plural(len(available), 'change')} most likely to matter in the "
                    f"{_plural(len(packages), 'package')} your code imports (up to "
                    f"{p.per_package} per package; --per-package N for more)."
                )
        return text + " Nothing written."
    return (
        f"{name} is up to date: {_plural(p.notes, 'note')} for {basis_text(p.model, p.cutoff)}, "
        f"versions from {p.versions_from}. Nothing written."
    )


def out_of_date_text(p: Proposal) -> str:
    """``sync --check``'s line for a file whose block would change."""
    name = p.target.name
    if p.target.block is None:
        why = f"it has no since-cutoff notes; {_plural(p.notes, 'note')} to write"
    else:
        why = "; ".join(c.state for c in p.changes)
    return f"{name} is out of date: {why}. Run {SYNC}."


def written_text(p: Proposal) -> str:
    """What sync says about a file it wrote."""
    name = p.target.name
    count = _plural(p.notes, "note")
    done = "; ".join(c.done for c in p.changes)
    if p.action == "created":
        return f"Created {name} with {count}."
    if p.action == "appended to":
        return f"Added the notes block to the end of {name}: {count}."
    if p.action == "removed from":
        return f"Removed the notes block from {name}: {done or 'no notes left'}."
    if p.action == "deleted":
        return f"Removed {name}: {done or 'no notes left'}."
    if p.action == "emptied":
        return (
            f"Removed the notes from {name}: {done or 'no notes left'}. Your own text in it "
            "stays, with since-cutoff's markers, which sync writes the notes between again "
            "when the package has some."
        )
    return f"Updated {name}: {done or count}."


def edited_text(p: Proposal) -> str:
    return (
        f"The since-cutoff block in {p.target.name} was edited by hand; sync would replace it "
        f"(diff above). Move your text below `{BLOCK_END}`, or run `since-cutoff sync --force`."
    )


def basis_text(model: str, cutoff: date) -> str:
    """``claude-sonnet-4-5 (cutoff 2025-07-31)``; several models, or a cutoff alone."""
    names = model_names(model)
    day = cutoff.isoformat()
    if len(names) > 1:
        return f"{_joined(names)} (earliest cutoff {day})"
    return f"{model} (cutoff {day})" if model else f"the cutoff {day}"


def after_text(model: str, cutoff: date) -> str:
    """``claude-sonnet-4-5's training cutoff (2025-07-31)``, as scan says it."""
    names = model_names(model)
    day = cutoff.isoformat()
    if len(names) > 1:
        return f"the earliest training cutoff of {_joined(names)} ({day})"
    return f"{model}'s training cutoff ({day})" if model else f"the cutoff ({day})"


# ---------------------------------------------------------------- status
@dataclass
class PackageStatus:
    package: str
    notes_for: str  # the version the notes are for
    locked: str | None  # the version the project's files give it now (None: none)
    versions_from: str | None  # the file that gives it (uv.lock, pyproject.toml, installed)
    state: str  # CURRENT, OUT_OF_DATE, ORPHAN, UNPINNED

    def to_dict(self) -> dict[str, Any]:
        return {
            "package": self.package,
            "notes_for": self.notes_for,
            "locked": self.locked,
            "versions_from": self.versions_from,
            "state": self.state,
        }


@dataclass
class DetectedModel:
    """The model the coding agent is set up with (hosts.detect_model), resolved offline."""

    target: ModelTarget
    spec: str  # as --model takes it
    source: str  # the setting it was read from


@dataclass
class TargetStatus:
    target: TargetFile
    packages: list[PackageStatus] = field(default_factory=list)
    problems: list[tuple[str, str]] = field(default_factory=list)  # (kind, text)
    notes: list[str] = field(default_factory=list)  # said, but not out of date

    @property
    def state(self) -> str:
        if self.target.block is None:
            return NEVER_RUN
        return OUT_OF_DATE if self.problems else CURRENT

    def to_dict(self) -> dict[str, Any]:
        block, meta = self.target.block, self.target.meta
        return {
            "file": self.target.name,
            "state": self.state,
            "format": block.version if block is not None else None,
            "model": meta.get("model"),
            "cutoff": meta.get("cutoff"),
            # As results.json and ``sync --json`` name it (the meta line keeps it as ``margin``).
            "cutoff_margin": block_margin(meta) if block is not None else None,
            "tool": meta.get("tool"),
            "scope": meta.get("scope"),
            "versions_from": meta.get("versions_from"),
            # A rule file's front matter counts too (TargetFile.edited); a 0.3 block has no
            # hash to tell (None).
            "edited": (
                None
                if block is None
                else self.target.edited
                if self.target.rule is not None
                else block.edited
            ),
            "packages": [p.to_dict() for p in self.packages],
            "problems": [{"kind": k, "text": t} for k, t in self.problems],
            "notes": list(self.notes),
        }


def current_deps_hash(project: Project) -> str:
    """The ``deps`` hash sync would write for the project as it is (no network)."""
    return deps_hash(dependency_pairs(scanned_dependencies(project)))


def target_status(
    project: Project,
    target: TargetFile,
    *,
    deps: str | None = None,
    detected: DetectedModel | None = None,
    margin: int = DEFAULT_CUTOFF_MARGIN,
) -> TargetStatus:
    """Whether the block in ``target`` is current for the project's dependencies as they are,
    without the network: the version of each package it has notes for, the hash of the
    dependencies (``deps``: :func:`current_deps_hash`), where the versions come from, how many
    days before the cutoff the comparison releases were published by (``margin``, the scan's
    ``--cutoff-margin``; the block says its own), and, when the coding agent's model is known
    (``detected``), its cutoff."""
    status = TargetStatus(target)
    block = target.block
    if block is None:
        return status
    by_key = {d.key: d for d in project.dependencies}
    for name, section in block.packages.items():
        dep = by_key.get(name)
        if dep is None:
            state, locked, where = ORPHAN, None, None
            status.problems.append((ORPHANED, f"{name} is no longer a dependency"))
        elif dep.version is None:
            state, locked, where = UNPINNED, None, None
            status.notes.append(
                f"{name} is not pinned, so its notes are checked against PyPI by {SYNC} only"
            )
        else:
            locked, where = dep.version, dep.pinned_in or dep.source
            state = CURRENT if _same_version(locked, section.version) else OUT_OF_DATE
            if state == OUT_OF_DATE:
                status.problems.append(
                    (STALE_VERSION, f"{name} {section.version} in the notes, {locked} in {where}")
                )
        status.packages.append(PackageStatus(name, section.version, locked, where, state))
    meta = block.meta
    if block.version < BLOCK_VERSION:
        # `sync --check` says so too: sync rewrites it whatever else it finds.
        status.problems.append(
            (OLD_FORMAT, f"written by since-cutoff 0.3: {SYNC} upgrades the block")
        )
    else:
        if meta.get("scope") == SCOPE_FAILURES:
            status.problems.append(
                (
                    RUN_BLOCK,
                    "written by `since-cutoff run` for what it found the model getting wrong: "
                    f"{SYNC} writes the notes for the changed APIs your code uses, keeping its "
                    "[type-checked] notes",
                )
            )
        deps = deps or current_deps_hash(project)
        if meta.get("deps") != deps and not status.problems:
            status.problems.append(
                (
                    DEPENDENCIES_CHANGED,
                    f"other dependencies changed since the notes were written; {SYNC} checks them",
                )
            )
        if meta.get("versions_from") != project.version_source and not status.problems:
            status.problems.append(
                (
                    VERSIONS_FROM_CHANGED,
                    f"the versions now come from {project.version_source}; the notes say "
                    f"{meta.get('versions_from')}",
                )
            )
        if block_margin(meta) != margin and not status.problems:
            status.problems.append(
                (
                    MARGIN_CHANGED,
                    f"the notes compare from {margin_text(block_margin(meta))}; the scan now "
                    f"compares from {margin_text(margin)} (--cutoff-margin {margin})",
                )
            )
    changed = _model_change(target, detected)
    if changed:
        status.notes.append(changed)
    if target.edited:
        status.notes.append(
            f"edited by hand since since-cutoff wrote it: {SYNC} asks for --force before "
            "replacing it"
        )
    return status


def _model_change(target: TargetFile, detected: DetectedModel | None) -> str | None:
    """What to say of a block written for a later training cutoff than that of the coding
    agent's model: the notes may miss changes between the two dates. Said, not a reason the
    block is out of date: sync keeps the block's model, so that teammates whose agents use
    other models do not rewrite it back and forth, and only ``sync --model`` changes it.

    Only the cutoff decides which changes the notes hold: a model with the same cutoff, or a
    later one (the notes then hold changes it may know already), is no change; nor is a
    block written for a date alone (``--cutoff``).
    """
    basis = target.basis
    if detected is None or basis is None or not basis[0]:
        return None
    model, cutoff = basis
    found = detected.target
    if found.cutoff >= cutoff or found.model_id in model_names(model):
        return None
    return (
        f"the notes are for {basis_text(model, cutoff)}; your coding agent is set up with "
        f"{basis_text(found.model_id, found.cutoff)} (from {detected.source}), which may also "
        f"miss changes from before {cutoff.isoformat()}: `since-cutoff sync --model "
        f"{detected.spec}` writes the notes for it"
    )


def note_import_tip(root: Path, statuses: Sequence[TargetStatus]) -> None:
    """Say in ``status`` what ``scan`` and ``sync`` say (notes.agents_import_tip) when CLAUDE.md
    does not import AGENTS.md: that both files have the block and how to keep one copy, or
    that Claude Code does not read the block in AGENTS.md; and when it does import it and both
    files have the block, that CLAUDE.md's is a second copy. Added to the AGENTS.md block's
    notes, so the text and ``status --json`` both have it (#13)."""
    tip = agents_import_tip(root, [s.target.path for s in statuses])
    if tip is None:
        return
    for s in statuses:
        if s.target.path == root / "AGENTS.md" and s.state != NEVER_RUN:
            s.notes.append(tip)


def exit_code(statuses: Sequence[TargetStatus]) -> int:
    """``status``: 3 when a block is out of date (as ``sync --check`` would find it, as far as
    that can be told offline), else 0; also when there is no block, which is no reason to fail:
    whether the code uses a changed API is ``sync --check``'s to say."""
    return EXIT_OUT_OF_DATE if any(s.state == OUT_OF_DATE for s in statuses) else EXIT_OK


def status_lines(project: Project, statuses: Sequence[TargetStatus]) -> list[str]:
    """What ``since-cutoff status`` prints."""
    out: list[str] = []
    for s in statuses:
        t = s.target
        if s.state == NEVER_RUN:
            out.append(
                f"{t.name}: no since-cutoff notes. {SYNC} writes them if your code uses an API "
                "that changed after the cutoff (`since-cutoff sync --check` says whether it "
                "does)."
            )
            continue
        basis = t.basis
        who = basis_text(*basis) if basis else "an unknown model"
        tool = t.meta.get("tool")
        by = f", written by since-cutoff {tool}" if isinstance(tool, str) else ""
        out.append(f"{t.name}: notes for {who}{by}")
        rows = [
            (
                p.package,
                f"notes for {p.notes_for}",
                f"{p.versions_from}: {p.locked}" if p.locked else "",
                STATE_TEXT[p.state],
            )
            for p in s.packages
        ]
        widths = [max((len(row[i]) for row in rows), default=0) for i in range(3)]
        for row in rows:
            cells = [cell.ljust(width) for cell, width in zip(row, widths, strict=False)]
            out.append("  " + "    ".join([*cells, row[3]]))
        out += [f"  ({text})" for kind, text in s.problems if kind not in (STALE_VERSION, ORPHANED)]
        out += [f"  ({text})" for text in s.notes]
    code = exit_code(statuses)
    if all(s.state == NEVER_RUN for s in statuses):
        out.append("No notes to check.")
    elif code == EXIT_OK:
        out.append(f"Current: the notes match {versions_phrase(project.version_source)}.")
    else:
        out.append(f"Out of date: run {SYNC} (exit code 3).")
    return out


def hook_line(statuses: Sequence[TargetStatus]) -> str | None:
    """``status --hook``: one line when a block is out of date, else None (also when there is
    no block: a session start should not nag about notes nobody asked for)."""
    stale = [s for s in statuses if s.state == OUT_OF_DATE]
    if not stale:
        return None
    names = _joined([s.target.name for s in stale])
    why = "; ".join(text for _, text in stale[0].problems)
    return (
        f"since-cutoff: the library notes in {names} are out of date: {why}. {SYNC} updates them."
    )


def sync_json(
    proposals: Sequence[Proposal], code: int, written: set[str], *, tip: str | None = None
) -> dict[str, Any]:
    """Describe proposals separately from writes, including check/dry-run refusals; ``tip``
    is what sync says about AGENTS.md and CLAUDE.md (notes.agents_import_tip), or None."""
    return {
        "exit_code": code,
        "tip": tip,
        "targets": [
            {
                "target": p.target.name,
                "action": p.action,
                "changed": p.changed,
                "edited": p.edited,
                "written": p.target.name in written,
                "model": p.model,
                "cutoff": p.cutoff.isoformat(),
                "cutoff_margin": p.margin,
                "scope": p.scope,
                "notes": p.notes,
                "changes": [
                    {"package": c.package, "done": c.done, "state": c.state} for c in p.changes
                ],
                "retest": p.retest,
                "diff_lines": list(diff_lines(p)),
            }
            for p in proposals
        ],
    }


def status_json(
    project: Project, statuses: Sequence[TargetStatus], detected: DetectedModel | None
) -> dict[str, Any]:
    """``status --json``."""
    code = exit_code(statuses)
    states = {s.state for s in statuses}
    state = NEVER_RUN if states == {NEVER_RUN} else CURRENT if code == EXIT_OK else OUT_OF_DATE
    found = detected.target if detected is not None else None
    return {
        "state": state,
        "exit_code": code,
        "versions_from": project.version_source,
        "coding_agent_model": None
        if found is None or detected is None
        else {
            "model": found.model_id,
            "cutoff": found.cutoff.isoformat(),
            "spec": detected.spec,
            "source": detected.source,
        },
        "targets": [s.to_dict() for s in statuses],
    }


OTHER_DEPENDENCIES = "other dependencies"


def lockfile_changes(project: Project, status: TargetStatus) -> str | None:
    """``uv.lock changed since the notes in AGENTS.md were written: anthropic 1.8.0 -> 1.9.2,
    huggingface-hub removed`` (sync says it before it scans), or None."""
    items = _lockfile_items(status)
    return _lockfile_text(project, [status.target.name], items) if items else None


def lockfile_notices(project: Project, statuses: Sequence[TargetStatus]) -> list[str]:
    """:func:`lockfile_changes` for several files, each change said once: the files with the
    same changes in one line, and all of a format's rule files by their folder. A rule file
    whose only change is in other dependencies (its package kept its version) is left out
    when another file says what changed: every rule file's deps hash covers every
    dependency, so one bump made a line for each of them."""
    found = [(s, _lockfile_items(s)) for s in statuses]
    found = [(s, items) for s, items in found if items]
    if any(items != [OTHER_DEPENDENCIES] for _, items in found):
        found = [
            (s, items)
            for s, items in found
            if not (s.target.rule is not None and items == [OTHER_DEPENDENCIES])
        ]
    groups: dict[tuple[str, ...], list[TargetFile]] = {}
    for s, items in found:
        groups.setdefault(tuple(items), []).append(s.target)
    every: dict[str, list[TargetFile]] = {}
    for s in statuses:
        if s.target.rule is not None:
            every.setdefault(s.target.rule.name, []).append(s.target)
    out = []
    for said, targets in groups.items():
        names: list[str] = []
        for t in targets:
            rule = t.rule
            if (
                rule is not None
                and len(every[rule.name]) > 1
                and every[rule.name] == [u for u in targets if u.rule is rule]
            ):
                name = f"the rule files in {rule.folder}"
            else:
                name = t.name
            if name not in names:
                names.append(name)
        out.append(_lockfile_text(project, names, list(said)))
    return out


def _lockfile_items(status: TargetStatus) -> list[str]:
    items = [
        f"{p.package} {p.notes_for} -> {p.locked}"
        if p.state == OUT_OF_DATE
        else f"{p.package} removed"
        for p in status.packages
        if p.state in (OUT_OF_DATE, ORPHAN)
    ]
    if not items and any(kind == DEPENDENCIES_CHANGED for kind, _ in status.problems):
        items.append(OTHER_DEPENDENCIES)
    return items


def _lockfile_text(project: Project, names: Sequence[str], items: Sequence[str]) -> str:
    word = project.versions_word
    word = "Your" + word[4:] if word.startswith("your") else word
    return f"{word} changed since the notes in {_joined(names)} were written: {', '.join(items)}"


# ---------------------------------------------------------------- helpers
def versions_phrase(version_source: str) -> str:
    """Where the versions come from, every source named: ``the versions in uv.lock``, ``the
    versions in requirements-dev.txt, requirements.txt``, ``the versions in pyproject.toml,
    latest on PyPI for 3 unpinned``, ``the versions in your virtual environment``; ``your
    dependencies`` when PyPI's latest releases stand in for every pin."""
    first, *rest = [part.strip() for part in version_source.split(",")]
    if not first or first.startswith("latest on PyPI"):
        return "your dependencies"
    head = "your virtual environment" if first in VENV_DIRS else first
    return "the versions in " + ", ".join([head, *rest])


def _line(bullet: tuple[str, tuple[str, ...]]) -> str:
    text, tags = bullet
    return f"{text} {tag_text(tags)}".strip()


def _counts(
    added: list[str],
    gone: list[str],
    apis: Mapping[str, Sequence[str]] | None = None,
    package: str | None = None,
) -> str:
    """``1 added, 1 dropped``; an old bullet that names the API of a new one (``apis``: the
    APIs each new bullet names, Note.apis) counts as ``changed`` instead of dropped, and so
    does every old bullet whose API a new bullet names when it names several (two bullets
    that merged into one: ``2 changed``). A new bullet about ``package`` as a whole (the one
    of a package first released after the cutoff, notes.new_package_note) stands for every
    old bullet of the section."""
    gone = list(gone)
    fresh = []
    changed = 0
    for line in added:
        names = list((apis or {}).get(line) or ())
        whole = package is not None and package in names
        olds = [g for g in gone if whole or any(_names(api, g) for api in names)]
        if not olds:
            fresh.append(line)
            continue
        for old in olds:
            gone.remove(old)
        changed += len(olds)
    if not fresh and not gone and not changed:
        return "text unchanged"
    parts = []
    if fresh:
        parts.append(f"{len(fresh)} added")
    if changed:
        parts.append(f"{changed} changed")
    if gone:
        parts.append(f"{len(gone)} dropped")
    return ", ".join(parts)


def _names(api: str, bullet: str) -> bool:
    """Does ``bullet`` name ``api`` as a note does: ```Messages.create()```, ```pkg.Thing```
    (not another API whose name starts the same)?"""
    return re.search(rf"`{re.escape(api)}[`(]", bullet) is not None


def _same_version(a: str, b: str) -> bool:
    try:
        return Version(a) == Version(b)
    except InvalidVersion:
        return a == b


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _joined(items: Sequence[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} and {items[-1]}"
