"""``since-cutoff explain API`` and the MCP tool ``explain_api`` (issue #28): everything
since-cutoff knows about one API, from the scan ``sync`` runs, with no model call and nothing
written.

For the API as a note names it (``Client.send``, ``huggingface_hub.hf_hub_download``), or the
end of that name (``send``), or ``package.API``: the note as the block writes it (the block's
own ``[type-checked]`` bullet when it has one, and the bullet it shares with other APIs, when
it does), which files' blocks have it and whether theirs is the note the scan gives now, the
package with the release at the cutoff, the pinned one and where each version comes from, each
change with both signatures and the changed parameter's declaration in each, the evidence
behind the tag (the library's own deprecation text with the file it is in, a replacement and
its source, what a move kept, a switched dependency's Requires-Dist), the "Runtime:" line, the
changelog, every place the project's code uses it with the form (file:line once the scan
records lines, issue #8), and what the tag does not check. A name that matches no changed API
lists the closest names, a note a block still has for it, and the packages that could not be
checked.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from packaging.utils import canonicalize_name

from since_cutoff.apidiff import DEPENDENCY_SWITCHED, MOVED, PARAM_REMOVED, APIChange
from since_cutoff.engine import (
    CHANGED,
    NEW,
    SKIPPED,
    Engine,
    ModelTarget,
    PackageScan,
    ScanResult,
    UsedAPI,
)
from since_cutoff.errors import METAPACKAGE, PackageIndexError, SinceCutoffError
from since_cutoff.notes import (
    NEW_PACKAGE,
    SCOPE_IMPORTED,
    SCOPE_USED,
    TAG_TYPE_CHECKED,
    Note,
    ParsedBlock,
    api_id,
    dependency_detail,
    diff_notes,
    rename_text,
    runtime_check,
    similar_text,
    split_tags,
    tag_legend,
    tag_text,
)
from since_cutoff.project import Project
from since_cutoff.pypi import changelog_url
from since_cutoff.report import place_form, places, use_text
from since_cutoff.selection import FORM_LABELS, type_project

# More matches than this: their names are listed, to pick one (exit code 1).
MATCHES_SHOWN = 3
CLOSE_MATCHES = 6

# How a name matched (Lookup.match, as --json says it), and how the text says it.
EXACT = "exact"
SUFFIX = "end_of_name"
EXACT_ANY_CASE = "exact_ignoring_case"
SUFFIX_ANY_CASE = "end_of_name_ignoring_case"
MATCH_TEXT = {
    EXACT: "by name",
    SUFFIX: "by the end of the name",
    EXACT_ANY_CASE: "by name, ignoring case",
    SUFFIX_ANY_CASE: "by the end of the name, ignoring case",
}

# How a block has an API's note (InBlock.how): the note the scan gives (or the bullet it
# shares), the block's own [type-checked] bullet (``run --apply``), or another note: one the
# scan no longer gives (written for another version, cutoff or margin).
FROM_DIFF = "diff"
TYPE_CHECKED = "type_checked"
OTHER = "other"


@dataclass
class Found:
    """One API since-cutoff has a note for, with what the scan knows of it."""

    note: Note  # the note from the diff for this API alone (Note.covered: its changes)
    package: PackageScan
    used: UsedAPI | None  # where the project's code uses it; None when it does not

    @property
    def api(self) -> str:
        return self.note.api

    def names(self) -> set[str]:
        """Every name the API goes by: the note's, each public path of each change (re-exports
        too), each of them after the package's names (``toylib.Client.send``), and a path
        with the PyPI name for its top module (``huggingface-hub.hf_hub_download``)."""
        c = self.note.change
        names = {self.note.api, *self.note.apis}
        for change in self.note.covered:
            names.update(p for p in (change.path, *(change.import_paths or ())) if p)
            if change.kind == DEPENDENCY_SWITCHED:
                names.update(n for n in (change.name, change.switched_to) if n)
        modules = {c.package.replace("-", "_"), *self.package.import_names}
        roots = {c.package, *modules}
        names.update(f"{r}.{self.note.api}" for r in roots)
        for n in list(names):
            head, dot, tail = n.partition(".")
            if dot and head in modules and head != c.package:
                names.add(f"{c.package}.{tail}")
        return names


@dataclass
class Lookup:
    """What a name finds: the APIs it names (and how it matched), else names close to it."""

    query: str
    found: list[Found] = field(default_factory=list)
    close: list[str] = field(default_factory=list)
    match: str | None = None  # EXACT, SUFFIX, ...; None when nothing matched

    @property
    def too_many(self) -> bool:
        """More matches than :data:`MATCHES_SHOWN`: only their names are listed."""
        return len(self.found) > MATCHES_SHOWN

    @property
    def explained(self) -> list[Found]:
        return [] if self.too_many else list(self.found)


@dataclass(frozen=True)
class Given:
    """``explain --package P --from A --to B``: which of the two releases were given rather
    than found, and the names of the options that give them (the MCP tool's arguments are
    ``from_version`` and ``to_version``)."""

    from_version: bool = False
    to_version: bool = False
    from_flag: str = "--from"
    to_flag: str = "--to"

    @property
    def any(self) -> bool:
        return self.from_version or self.to_version


@dataclass
class InBlock:
    """A block's bullet for an API (:func:`in_blocks`)."""

    file: str
    how: str  # FROM_DIFF, TYPE_CHECKED or OTHER
    bullet: str  # as the block has it, with its tag
    version: str  # the version of the package the block's section is for
    compared: str | None = None  # the release its section compares from
    current: bool = True  # OTHER: written for the scan's model, cutoff and margin

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "how": self.how,
            "bullet": self.bullet,
            "version": self.version,
            "compared_from": self.compared,
        }


def candidates(scan: ScanResult, package: str | None = None) -> list[Found]:
    """Every API of the scan's changed packages with a note: those the code uses first (with
    their places), then the others (each from the diff alone), then a package first released
    after the cutoff that the code imports (its ``[metadata]`` note). ``package`` keeps one."""
    wanted = canonicalize_name(package) if package else None
    out: list[Found] = []
    seen: set[str] = set()
    for u in scan.used_apis():
        if wanted is None or u.package.name == wanted:
            out.append(Found(u.note, u.package, u))
            seen.update(c.api_key for c in u.note.covered)
    for p in scan.packages:
        if p.status != CHANGED or (wanted is not None and p.name != wanted):
            continue
        rest = [c for c in p.distinct if c.api_key not in seen]
        out += [Found(n, p, None) for n in diff_notes(rest, changelog=p.changelog)]
    by_name = {p.name: p for p in scan.packages}
    for n in scan.new_package_notes():
        if wanted is None or n.change.package == wanted:
            out.append(Found(n, by_name[n.change.package], None))
    return out


def clean_name(query: str) -> str:
    """The name in ``query`` without what surrounds it in a note: backticks and call syntax
    (```Client.send()```, ``Client.send(timeout=Timeout(5))``), whichever comes first."""
    q = query.strip()
    while True:
        before = q
        q = _without_call(q.strip().strip("`").strip()).strip()
        if q == before:
            return q


def _without_call(q: str) -> str:
    """``q`` without the call at its end: ``Client.send(timeout=Timeout(5))`` is
    ``Client.send``; ``q`` as it is when its brackets do not balance."""
    if not q.endswith(")"):
        return q
    depth = 0
    for i in range(len(q) - 1, -1, -1):
        if q[i] == ")":
            depth += 1
        elif q[i] == "(":
            depth -= 1
            if depth == 0:
                return q[:i]
    return q


def _tests(q: str) -> tuple[tuple[str, Callable[[str], bool]], ...]:
    """How a name matches ``q``, in order: exactly, by the end of a name (``send`` for
    ``Client.send``), then either without regard to case."""
    low = q.lower()
    return (
        (EXACT, lambda n: n == q),
        (SUFFIX, lambda n: n.endswith("." + q)),
        (EXACT_ANY_CASE, lambda n: n.lower() == low),
        (SUFFIX_ANY_CASE, lambda n: n.lower().endswith("." + low)),
    )


def lookup(scan: ScanResult, query: str, package: str | None = None) -> Lookup:
    """The APIs ``query`` names (:func:`_tests`; :func:`clean_name` first). None found: the
    names closest to it."""
    q = clean_name(query)
    result = Lookup(query.strip())
    if not q:
        return result
    found = candidates(scan, package)
    for kind, test in _tests(q):
        hits = [f for f in found if any(test(n) for n in f.names())]
        if hits:
            result.found, result.match = hits, kind
            return result
    apis = sorted({f.api for f in found})
    close = difflib.get_close_matches(q, apis, n=CLOSE_MATCHES, cutoff=0.5)
    inside = [a for a in apis if q.lower() in a.lower() and a not in close]
    tail = [a for a in apis if a.rsplit(".", 1)[-1].lower() == q.rsplit(".", 1)[-1].lower()]
    result.close = list(dict.fromkeys([*close, *inside, *tail]))[:CLOSE_MATCHES]
    return result


GIVEN = "as given"  # PackageScan.version_source of a version_scan with --to


def version_scan(
    engine: Engine,
    project: Project,
    target: ModelTarget,
    package: str,
    from_version: str | None = None,
    to_version: str | None = None,
) -> ScanResult:
    """A scan of one package between two releases (``explain --package P --from A --to B``):
    ``from_version`` else the latest release by the target's comparison day, ``to_version``
    else the version the project pins, else the latest on PyPI. The project's code is still
    read for where it uses the changed APIs. SinceCutoffError when there is nothing to compare,
    PackageIndexError when it cannot be diffed."""
    name = canonicalize_name(package)
    pypi = engine.pypi
    dep = next((d for d in project.dependencies if d.key == name), None)
    to = (to_version or "").strip() or (dep.version if dep is not None else None)
    new = pypi.release(name, to) if to else pypi.latest(name)
    if from_version and from_version.strip():
        old = pypi.release(name, from_version.strip())
    else:
        found = pypi.version_at(name, target.compare_date)
        if found is None:
            raise SinceCutoffError(
                f"{name} had no release {target.compare_when} ({target.compare_date}): give "
                "the release to compare from with --from"
            )
        old = found
    if new.parsed <= old.parsed:
        raise SinceCutoffError(
            f"{name} {new.version} is not newer than {old.version}: there is no change to explain"
        )
    where = (
        GIVEN
        if (to_version or "").strip()
        else (dep.pinned_in or dep.source)
        if dep is not None and dep.version
        else "latest on PyPI"
    )
    scan = PackageScan(
        name,
        new.version,
        where,
        dep.direct if dep is not None else False,
        status=CHANGED,
        cutoff_version=old.version,
        cutoff_version_date=old.uploaded.date().isoformat(),
        locked_date=new.uploaded.date().isoformat(),
        changelog=changelog_url(pypi.project_urls(name)),
    )
    engine.diff_package(scan)
    if scan.status == SKIPPED:
        raise PackageIndexError(scan.reason or f"could not diff {name}")
    scan.imported = project.imports(scan.import_names) if scan.import_names else None
    typed = type_project(project, [scan], pypi, engine.store)
    return ScanResult(typed, target, [scan], name_matches=engine.settings.include_name_matches)


# --------------------------------------------------------------- the blocks
def merged_bullet(scan: ScanResult, f: Found) -> Note | None:
    """The note a block writes for this API when it shares one bullet with other APIs
    (notes.diff_notes with ``merge``; a block of ``--scope imported`` for an API the code does
    not use), else None."""
    keys = {c.api_key for c in f.note.covered}
    notes = scan.scope_notes(SCOPE_USED if f.used is not None else SCOPE_IMPORTED)
    for n in notes:
        if keys & {c.api_key for c in n.covered} and len(n.apis) > 1:
            return n
    return None


def in_blocks(scan: ScanResult, f: Found, blocks: Mapping[str, ParsedBlock]) -> list[InBlock]:
    """The blocks that have a bullet for this API, and how: FROM_DIFF when it is the note the
    scan gives for the block's scope and options (or the bullet that note shares with other
    APIs), TYPE_CHECKED for the block's own ``[type-checked]`` bullet (written by ``run
    --apply``, kept by ``sync``: the meta line's ``checked`` names its API), else OTHER for a
    bullet about it (it names the API first) that is not the scan's: written for another
    version, cutoff or margin, which ``sync`` would replace. A bullet that names the API only
    as another one's replacement is not about it."""
    out: list[InBlock] = []
    package = f.note.change.package
    keys = {c.api_key for c in f.note.covered}
    names = f.names()
    notes: dict[tuple[str, bool, int | None], list[Note]] = {}
    for file, block in blocks.items():
        section = block.packages.get(package)
        if section is None:
            continue
        checked = _type_checked(block, package, {api_id(c) for c in f.note.covered})
        if checked is not None:
            out.append(
                InBlock(file, TYPE_CHECKED, checked, section.version, section.cutoff_version)
            )
            continue
        lines = [_line(b) for b in section.bullets]
        expected = {n.line for n in _block_notes(scan, block, notes) if keys & _keys(n)}
        hit = next((line for line in lines if line in expected), None)
        if hit is not None:
            out.append(InBlock(file, FROM_DIFF, hit, section.version, section.cutoff_version))
            continue
        about = next((line for line in lines if _subjects(line) & names), None)
        if about is not None:
            current = _same_basis(scan, block)
            out.append(
                InBlock(file, OTHER, about, section.version, section.cutoff_version, current)
            )
    return out


def _block_notes(
    scan: ScanResult, block: ParsedBlock, cache: dict[tuple[str, bool, int | None], list[Note]]
) -> list[Note]:
    """The notes sync writes into ``block`` from this scan: for its scope, with its
    ``--suggestions`` and ``--per-package``."""
    meta = block.meta
    scope = meta.get("scope") if meta.get("scope") in (SCOPE_USED, SCOPE_IMPORTED) else SCOPE_USED
    per = meta.get("per_package")
    key = (str(scope), meta.get("suggestions") is True, per if isinstance(per, int) else None)
    if key not in cache:
        cache[key] = scan.scope_notes(key[0], suggestions=key[1], per_package=key[2])
    return cache[key]


def _keys(n: Note) -> set[str]:
    return {c.api_key for c in n.covered}


def _type_checked(block: ParsedBlock, package: str, ids: set[str]) -> str | None:
    """The block's ``[type-checked]`` bullet for the API (``ids``: notes.api_id), with its
    tag; the meta line's ``checked`` lists their APIs in the order of the bullets."""
    checked = [
        (name, _line((text, tags)))
        for name, section in block.packages.items()
        for text, tags in section.bullets
        if TAG_TYPE_CHECKED in tags
    ]
    listed = block.meta.get("checked")
    if not isinstance(listed, list) or len(listed) != len(checked):
        return None
    return next(
        (
            line
            for (name, line), i in zip(checked, listed, strict=True)
            if name == package and str(i) in ids
        ),
        None,
    )


def _same_basis(scan: ScanResult, block: ParsedBlock) -> bool:
    """Whether the block was written for the scan's model, cutoff and margin."""
    t, meta = scan.target, block.meta
    margin = meta.get("margin") if isinstance(meta.get("margin"), int) else 0
    return (
        (meta.get("model") or "") == (t.model_id or "")
        and meta.get("cutoff") == t.cutoff.isoformat()
        and margin == t.margin
    )


def _line(bullet: tuple[str, tuple[str, ...]]) -> str:
    text, tags = bullet
    return f"{text} {tag_text(tags)}".strip()


# The code spans a bullet starts with: the APIs it is about (```A`, `B` and `C` were removed``).
_LEADING = re.compile(r"(?:`[^`]+`(?:,? and |,? or |, ))*`[^`]+`")


def _subjects(bullet: str) -> set[str]:
    """The APIs a block's bullet is about: the code spans it starts with, without call
    syntax (```Client.send()``` is ``Client.send``)."""
    m = _LEADING.match(bullet)
    if m is None:
        return set()
    return {re.sub(r"\(.*", "", s).strip() for s in re.findall(r"`([^`]+)`", m.group())}


@dataclass
class Mention:
    """A block's bullet about an API that the scan finds no change for (:func:`mentions`)."""

    file: str
    package: str
    version: str
    bullet: str


def mentions(query: str, blocks: Mapping[str, ParsedBlock]) -> list[Mention]:
    """The bullets of the blocks about the API ``query`` names (:func:`_subjects`, matched as
    :func:`lookup` matches names), for a name the scan finds no change for: notes written for
    another version, or for a package that could not be checked."""
    q = clean_name(query)
    if not q:
        return []
    found: list[Mention] = []
    for _, test in _tests(q):
        for file, block in blocks.items():
            for package, section in block.packages.items():
                for bullet in section.bullets:
                    line = _line(bullet)
                    names = _subjects(line)
                    names |= {f"{package}.{n}" for n in names}
                    if any(test(n) for n in names):
                        found.append(Mention(file, package, section.version, line))
        if found:
            return found
    return found


def notices(project: Project, scan: ScanResult, files: Iterable[Any]) -> list[str]:
    """What explain says before the explanation, as ``sync`` says it before its scan: the
    lockfile changes since each block was written (sync.lockfile_changes), and the packages
    that could not be checked. ``files``: the target files (sync.TargetFile)."""
    from since_cutoff.sync import lockfile_notices, target_status

    statuses = [
        target_status(project, t, margin=scan.target.margin)
        for t in files
        if getattr(t, "block", None) is not None
    ]
    out = [
        f"{moved}: `since-cutoff sync` brings the notes in step."
        for moved in lockfile_notices(project, statuses)
    ]
    for p in scan.skipped:
        if p.reason and METAPACKAGE not in p.reason:
            out.append(f"{p.name} could not be checked ({p.reason}), so it has no notes here.")
    return out


# ------------------------------------------------------------------ the text
def _versions(scan: ScanResult, p: PackageScan, given: Given | None = None) -> str:
    given = given or Given()
    if p.status == NEW:
        first = f", first released {p.first_released}" if p.first_released else ""
        return (
            f"{p.name} {p.locked} (from {scan.versions_from(p)}){first}: no release by the cutoff "
            "to compare with"
        )
    pinned = f"{p.locked} ({p.locked_date})" if p.locked_date else f"{p.locked}"
    old = (
        f"{p.cutoff_version} ({p.cutoff_version_date})"
        if p.cutoff_version_date
        else str(p.cutoff_version)
    )
    to = f"given with {given.to_flag}" if given.to_version else f"from {scan.versions_from(p)}"
    since = (
        f"given with {given.from_flag}"
        if given.from_version
        else f"the latest release {scan.target.compare_when}"
    )
    return f"{p.name} {pinned}, {to}; compared from {old}, {since}"


def _change_lines(scan: ScanResult, c: APIChange, p: PackageScan) -> list[str]:
    if c.kind == NEW_PACKAGE:
        if p.first_released and scan.target.released_within_margin(p):
            when = (
                f"first released {p.first_released}, within {scan.target.margin} days of the cutoff"
            )
        elif p.first_released:
            when = f"first released {p.first_released}, after the cutoff"
        else:
            when = "first released after the cutoff"
        return [f"- new package: {when}; no release by the cutoff to compare with"]
    out = [f"- {c.kind}: {c.describe(versioned=False)}"]
    if c.old_signature:
        out.append(f"  {c.from_version}: {c.old_signature}")
    if c.new_signature:
        out.append(f"  {c.to_version}: {c.new_signature}")
    if c.parameter and (c.old_signature or c.new_signature):
        name = c.parameter.lstrip("*")
        before = _declared(c.old_signature, name)
        after = _declared(c.new_signature, name)
        if c.kind == PARAM_REMOVED and not after.startswith("`"):
            after = "removed"  # whether or not the signature shown reaches that far
        # A parameter that moved past `*` reads the same: its kind says what changed.
        if before != after or not before.startswith("`"):
            out.append(
                f"  parameter `{c.parameter}`: in {c.from_version}, {before}; in "
                f"{c.to_version}, {after}"
            )
    if c.hint:
        where = f" ({c.hint_file})" if c.hint_file else ""
        how = f", {c.hint_source}" if c.hint_source else ""
        out.append(f'  {c.package} {c.from_version} said{where}{how}: "{_one(c.hint)}"')
    if c.deprecation:
        where = f" ({c.deprecation_file})" if c.deprecation_file else ""
        out.append(f'  {c.package} {c.to_version} says{where}: "{_one(c.deprecation)}"')
    if c.still_handled_text:
        out.append(
            f'  {c.to_version}\'s code that still reads it says: "{_one(c.still_handled_text)}"'
        )
    if c.kind == MOVED and c.move_evidence:
        out.append(f"  move evidence: {_evidence(c.move_evidence)}")
    if c.kind == DEPENDENCY_SWITCHED:
        out.append(f"  {dependency_detail(c)}")
    for text in (rename_text(c), similar_text(c)):
        if text:
            out.append(f"  {text}")
    return out


def _parameters(signature: str) -> tuple[list[str], bool]:
    """The parameters of a signature as written (``temperature: float = 1.0``), split at the
    commas outside brackets and quotes, and whether the list ends (its ``)``): a signature cut
    at 400 characters may stop inside it."""
    start = signature.find("(")
    if start < 0:
        return [], True
    out, depth, quote, part, closed = [], 0, "", "", False
    for ch in signature[start + 1 :]:
        if quote:
            quote = "" if ch == quote else quote
        elif ch in "'\"":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                closed = True
                break
            depth -= 1
        elif ch == "," and depth == 0:
            out.append(part.strip())
            part = ""
            continue
        part += ch
    if part.strip():
        out.append(part.strip())
    return out, closed


def _declared(signature: str | None, name: str) -> str:
    """How ``signature`` declares the parameter ``name``: ```temperature: float = 1.0```, or
    why it does not show it (the signature is cut at 400 characters before it). A declaration
    the cut ends inside says so."""
    if not signature:
        return "no signature"
    parts, closed = _parameters(signature)
    cut = signature.endswith("...") and not closed
    for i, part in enumerate(parts):
        if re.match(rf"\*{{0,2}}{re.escape(name)}\b", part):
            if cut and i == len(parts) - 1:
                return f"`{part}` (the signature above is cut at 400 characters)"
            return f"`{part}`"
    if signature.endswith("..."):
        return "past the end of the signature above (cut at 400 characters)"
    return "not a parameter"


def _signature_heads(c: APIChange) -> tuple[str, ...]:
    return tuple(
        f"  {v}: {s}"
        for v, s in ((c.from_version, c.old_signature), (c.to_version, c.new_signature))
        if s
    )


def _evidence(e: Mapping[str, Any]) -> str:
    return ", ".join(f"{k} {v}" for k, v in e.items())


def _one(text: str, limit: int = 400) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _not_checked(note: Note, *, type_checked: bool = False, new: bool = False) -> list[str]:
    """What the note's tag does not say was checked (Note.checks); ``type_checked``: a block
    has a ``[type-checked]`` bullet for it; ``new``: a package first released after the
    cutoff, which has no earlier release to compare with."""
    checks = note.checks()
    out = []
    if checks["example_type_checks"] is None and not type_checked:
        out.append(
            "no example was type-checked against the pinned version (`since-cutoff run` does "
            "that, with a model)"
        )
    if new:
        out.append("nothing of the library was run, nor its API read: there is no diff")
    else:
        out.append("nothing of the library was run: its behaviour behind an unchanged signature")
    out.append("whether a model gets this API wrong was not measured (`since-cutoff run`)")
    return out


def explain_lines(
    scan: ScanResult,
    f: Found,
    blocks: Mapping[str, ParsedBlock] | None = None,
    given: Given | None = None,
) -> list[str]:
    """What ``explain`` prints for one API (plain text that reads as Markdown)."""
    note, p = f.note, f.package
    where = in_blocks(scan, f, blocks or {})
    checked = next((b for b in where if b.how == TYPE_CHECKED), None)
    out = [f"## {f.api} ({p.name})", "", "Note:", f"- {checked.bullet if checked else note.line}"]
    merged = merged_bullet(scan, f)
    if merged is not None and merged.line != note.line and checked is None:
        out += ["The block writes it in a bullet shared with other APIs:", f"- {merged.line}"]
    listed = [_written(b, p) for b in where if b.how != OTHER]
    if listed:
        out.append("Written in: " + "; ".join(listed) + ".")
    if checked is not None:
        out += [
            "A [type-checked] bullet was written by `since-cutoff run --apply`: its example "
            "and the type checker's verdict are in .since-cutoff/results.json. It stands in for "
            "the note from the diff:",
            f"- {note.line}",
        ]
    for b in where:
        if b.how != OTHER:
            continue
        if b.current:
            why = "`since-cutoff sync` writes the note above in its place"
        else:
            why = "written for another model, cutoff or --cutoff-margin than this explanation"
        out += [
            f"{b.file} has another note for it, for {p.name} {b.version} ({why}):",
            f"- {b.bullet}",
        ]
    if not where and blocks:
        out.append("Not in a notes block yet: `since-cutoff sync` writes it if the code uses it.")
    out += ["", f"Package: {_versions(scan, p, given)}."]
    if p.changelog:
        out.append(f"Changelog: {p.changelog}")
    out += ["", "Changes:"]
    shown: set[tuple[str | None, str | None]] = set()
    for c in note.covered:
        lines = _change_lines(scan, c, p)
        signatures = (c.old_signature, c.new_signature)
        if signatures in shown:  # the same API's signatures, said for its first change
            lines = [line for line in lines if not line.startswith(_signature_heads(c))]
        shown.add(signatures)
        out += lines
    tags = list(note.tag_list)
    if checked is not None:
        own = split_tags(checked.bullet)[1]
        tags = list(dict.fromkeys([*own, *tags]))
        out += [
            "",
            f"Evidence {tag_text(own)}, and {tag_text(note.tag_list)} for the note from the diff:",
        ]
    else:
        out += ["", f"Evidence {tag_text(tags)}:"]
    out += [f"- {line}" for line in tag_legend(tags)]
    checks = note.checks()
    out.append(f"- checked: {checks['change']}")
    if checked is not None:
        out.append(
            f"- example: type-checked against {p.name} {checked.version} by `since-cutoff run` "
            "(.since-cutoff/results.json)"
        )
    for r in note.replacements:
        replaces = f" for `{r.replaces}`" if r.replaces else ""
        out.append(f"- replacement `{r.text}`{replaces} ({r.evidence}): {r.source}")
    if note.not_confirmed:
        names = ", ".join(f"`{n}`" for n in note.not_confirmed)
        out.append(f"- similar names, not confirmed as replacements: {names}")
    out.append(f"- runtime: {runtime_check(note.covered)}")
    out += ["", "Where your code uses it:"]
    out += _uses_lines(scan, f)
    out += ["", "Not checked:"]
    out += [
        f"- {line}"
        for line in _not_checked(note, type_checked=checked is not None, new=p.status == NEW)
    ]
    return out


def _written(b: InBlock, p: PackageScan) -> str:
    """``AGENTS.md (from the diff)``; the same note in a section for other releases than the
    scan's says them."""
    if b.how == TYPE_CHECKED:
        return f"{b.file} ([type-checked])"
    if b.version != p.locked or (b.compared and b.compared != p.cutoff_version):
        since = f" compared from {b.compared}" if b.compared else ""
        return f"{b.file} (the same note, in its section for {p.name} {b.version}{since})"
    return f"{b.file} (from the diff)"


def _uses_lines(scan: ScanResult, f: Found) -> list[str]:
    p = f.package
    if f.used is not None:
        return [
            f"- {at}: {FORM_LABELS[place_form(uses)]}, {use_text(uses)}"
            for at, uses in places(f.used)
        ]
    if p.status == NEW:  # its note is for the files that import it
        files = _importing(scan, p)
        if files:
            return [f"- {name}: imports it" for name in files]
        return ["- nowhere: your code does not import it"]
    imported = "it imports the package" if p.imported else "nor does it import the package"
    return [f"- nowhere: your code does not use this API ({imported})"]


def _importing(scan: ScanResult, p: PackageScan) -> list[str]:
    from since_cutoff.sync import importing_files

    return importing_files(scan, p)


def _head(scan: ScanResult, given: Given | None) -> str:
    if given is not None and given.any and scan.packages:
        p = scan.packages[0]
        since = (
            given.from_flag
            if given.from_version
            else f"the latest release {scan.target.compare_when}"
        )
        to = given.to_flag if given.to_version else scan.versions_from(p)
        return (
            f"For {_basis(scan)}; {p.name} from {p.cutoff_version} ({since}) to {p.locked} ({to})."
        )
    return f"For {_basis(scan)}; versions from {scan.project.version_source}."


def lookup_lines(
    scan: ScanResult,
    result: Lookup,
    blocks: Mapping[str, ParsedBlock],
    *,
    given: Given | None = None,
    package: str | None = None,
    said: Sequence[str] = (),
) -> list[str]:
    """What ``explain`` prints for a lookup: each API it names (at most MATCHES_SHOWN; more
    are listed by name), or why nothing matched, with the closest names. ``said``: the
    :func:`notices` to print first; ``package``: ``--package``."""
    head = [f"# {result.query}", "", _head(scan, given), *said, ""]
    if not result.found:
        return [*head, *no_match_lines(scan, result, blocks, package, given)]
    how = MATCH_TEXT[result.match or EXACT]
    names = ", ".join(f"{f.api} ({f.package.name})" for f in result.found)
    if result.too_many:
        return [
            *head,
            f"{len(result.found)} APIs match '{result.query}' {how}: {names}. Give one of "
            "them (exit code 1).",
        ]
    out = head
    if len(result.found) > 1:
        out += [f"{len(result.found)} APIs match '{result.query}' {how}: {names}.", ""]
    elif result.match != EXACT:
        out += [f"'{result.query}' matches {names} {how}.", ""]
    for i, f in enumerate(result.found):
        if i:
            out.append("")
        out += explain_lines(scan, f, blocks, given)
    return out


def no_match_lines(
    scan: ScanResult,
    result: Lookup,
    blocks: Mapping[str, ParsedBlock],
    package: str | None = None,
    given: Given | None = None,
) -> list[str]:
    """Why nothing matched, and what is close: also a note a block still has for the name
    (written for another version: the notes are out of date), and a package that could not be
    checked, which the name may be in. ``--package`` for a package the project does not
    depend on says so, unless ``--from`` or ``--to`` scanned it all the same."""
    if _not_a_dependency(scan, package):
        return [f"{package} is not a dependency of this project, so explain has no API of it."]
    out = [no_match_text(scan, result, given)]
    found = mentions(result.query, blocks)
    for m in found:
        out += [
            f"{m.file} has a note for it, for {m.package} {m.version}, which this scan does not "
            "find: the notes may be out of date (`since-cutoff status`, `since-cutoff sync`).",
            f"- {m.bullet}",
        ]
    q = clean_name(result.query)
    head = q.split(".", 1)[0]
    noted = {m.package for m in found}
    for p in scan.skipped:
        if p.name in noted or canonicalize_name(head) == p.name or head in p.import_names:
            out.append(
                f"{p.name} could not be checked ({p.reason}): explain cannot say whether "
                f"'{result.query}' changed in it."
            )
    return out


def no_match_text(scan: ScanResult, result: Lookup, given: Given | None = None) -> str:
    """Why nothing matched, and what is close (between the two releases of ``--from`` and
    ``--to``, when they are given)."""
    close = f" Close matches: {', '.join(result.close)}." if result.close else ""
    if given is not None and given.any and scan.packages:
        p = scan.packages[0]
        return (
            f"No API named '{result.query}' changed in {p.name} between {p.cutoff_version} and "
            f"{p.locked}.{close}"
        )
    changed = [p.name for p in scan.packages if p.status in (CHANGED, NEW)]
    if not changed:
        return f"No API named '{result.query}': no dependency changed its API after {_basis(scan)}."
    return (
        f"No API named '{result.query}' changed in {', '.join(sorted(changed))} after "
        f"{_basis(scan)}.{close}"
    )


def _not_a_dependency(scan: ScanResult, package: str | None) -> bool:
    """``--package`` names a package the project does not depend on, and that the scan did not
    cover (``--from`` and ``--to`` scan one all the same)."""
    if not package:
        return False
    wanted = canonicalize_name(package)
    if any(p.name == wanted for p in scan.packages):
        return False
    return not any(d.key == wanted for d in scan.project.dependencies)


def _basis(scan: ScanResult) -> str:
    t = scan.target
    who = f"{t.model_id}'s training cutoff" if t.model_id else "the cutoff"
    return f"{who} ({t.cutoff.isoformat()})"


def exit_code(result: Lookup) -> int:
    """0 when the name explains one to MATCHES_SHOWN APIs; 1 when it names none, or so many
    that only their names are listed."""
    return 0 if result.explained else 1


# ------------------------------------------------------------------ the JSON
def explain_json(
    scan: ScanResult,
    result: Lookup,
    blocks: Mapping[str, ParsedBlock] | None = None,
    *,
    given: Given | None = None,
    package: str | None = None,
    said: Sequence[str] = (),
) -> dict[str, Any]:
    """``explain --json``: the same as the text, for each API the name finds (``found``, at
    most MATCHES_SHOWN; ``matches`` names them all), with the keys results.json uses for the
    same things (``api`` the full path, ``display`` the note's name, ``change_id``)."""
    t = scan.target
    blocks = blocks or {}
    return {
        "query": result.query,
        "model": t.model_id or None,
        "cutoff": t.cutoff.isoformat(),
        "cutoff_margin": t.margin,
        "versions_from": scan.project.version_source,
        "notices": list(said),
        "match": result.match,
        "matches": [{"display": f.api, "package": f.package.name} for f in result.found],
        "truncated": result.too_many,
        "found": [_found_json(scan, f, blocks, given) for f in result.explained],
        "close_matches": list(result.close),
        "block_mentions": (
            [m.__dict__ for m in mentions(result.query, blocks)] if not result.found else []
        ),
        "not_checked": [{"package": p.name, "reason": p.reason} for p in scan.skipped],
        "not_a_dependency": _not_a_dependency(scan, package),
        "exit_code": exit_code(result),
    }


def _found_json(
    scan: ScanResult, f: Found, blocks: Mapping[str, ParsedBlock], given: Given | None
) -> dict[str, Any]:
    p, note = f.package, f.note
    given = given or Given()
    merged = merged_bullet(scan, f)
    where = in_blocks(scan, f, blocks)
    checked = next((b for b in where if b.how == TYPE_CHECKED), None)
    return {
        "api": note.change.path,
        "display": f.api,
        "package": p.name,
        "version": p.locked,
        "cutoff_version": p.cutoff_version,
        "versions_from": f"given with {given.to_flag}"
        if given.to_version
        else scan.versions_from(p),
        "to_given": given.to_version,
        "from_given": given.from_version,
        "status": p.status,
        "changelog": p.changelog,
        "note": note.to_dict() | {"line": note.line, "tag_legend": tag_legend(note.tag_list)},
        "block_bullet": (
            checked.bullet
            if checked is not None
            else merged.line
            if merged is not None and merged.line != note.line
            else None
        ),
        "in_blocks": [b.to_dict() for b in where],
        "changes": [_change_json(c) for c in note.covered],
        "runtime": runtime_check(note.covered),
        "uses": _uses_json(scan, f),
        "not_checked": _not_checked(note, type_checked=checked is not None, new=p.status == NEW),
    }


def _change_json(c: APIChange) -> dict[str, Any]:
    """The change as results.json's ``diff`` writes it (APIChange.to_dict: ``id``...), with
    ``change_id`` as its ``used_apis`` name it, the description the text gives, and a switched
    dependency's Requires-Dist line."""
    d = c.to_dict()
    d["change_id"] = c.id
    d["description"] = c.describe(versioned=False) if c.kind != NEW_PACKAGE else None
    if c.kind == DEPENDENCY_SWITCHED:
        d["dependency_detail"] = dependency_detail(c)
    return d


def _uses_json(scan: ScanResult, f: Found) -> list[dict[str, Any]]:
    if f.used is None:
        if f.package.status == NEW:
            return [
                {
                    "file": name,
                    "line": None,
                    "column": None,
                    "form": "imports",
                    "what": "imports it",
                    "match": None,
                }
                for name in _importing(scan, f.package)
            ]
        return []
    return [
        {
            "file": u.file,
            "line": u.line,
            "column": u.column,
            "form": u.form,
            "what": use_text([u]),
            "match": u.how,
        }
        for u in f.used.uses
    ]


def blocks_of(files: Iterable[Any]) -> dict[str, ParsedBlock]:
    """The blocks of the target files (sync.TargetFile) that have one, by name."""
    return {t.name: t.block for t in files if getattr(t, "block", None) is not None}
