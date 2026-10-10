"""Notes for AGENTS.md / CLAUDE.md: short bullets about the library changes a coding model may
not know, each tagged with what was checked, and nothing else claimed.

- **From the API diff** (:func:`diff_note`, no model call): one bullet per API, for every
  change of it. A replacement is named only with evidence, and the tag says which:

  ============================  ==========================================================
  ``[diff]``                    the change is in the static comparison of the two releases'
                                public APIs; no replacement is named ("do not pass it")
  ``[diff + library]``          the replacement is named in the library's own deprecation
                                text and exists in the pinned version (``use `stop```)
  ``[diff + move checked]``     the object at the new path keeps the old one's public names
                                or parameters (:func:`apidiff._Differ.find_moved`)
  ``[diff; probable rename]``   a parameter in the same position, with the same annotation,
                                has a new name: a guess, labelled as one
  ``[diff + metadata]``         the older release's Requires-Dist lists a distribution the
                                pinned one does not, and the public API that named its types
                                names another required distribution's types instead
                                (:func:`dependency_bullet`)
  ``[not confirmed]``           names that merely look similar; only with ``suggestions``
  ============================  ==========================================================

- **Written by a model** in ``since-cutoff run`` (``[type-checked]``): kept only when its example
  type-checks against the exact version the project uses and every identifier the bullet
  recommends appears in that example or in the diff; otherwise the diff's bullet.

The block (:func:`render_block`, format 2) starts with 0.3's marker, then a meta line (model,
cutoff, where the versions come from, hashes of the dependencies and of the block's own text,
so a hand edit shows), a header that says what the tags mean, and the bullets by package, each
package with the version they apply to. :func:`parse_block` reads it back, and 0.3's blocks too.
:func:`block_targets` says which files get it, and :func:`with_block` / :func:`without_block`
what a file's text becomes, leaving every byte outside the markers as it is (``sync`` shows
the diff before :func:`apply_block` writes it).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from since_cutoff import __version__
from since_cutoff.apidiff import (
    DEPENDENCY_SWITCHED,
    DEPRECATED,
    DIFF_SCHEMA,
    HINT_PARAM_DOC,
    HINT_WARNING,
    KIND_CHANGED,
    KIND_PRIORITY,
    MOVED,
    PARAM_KEYWORD_ONLY,
    PARAM_POSITIONAL_ONLY,
    PARAM_REMOVED,
    PARAM_REQUIRED,
    REMOVED,
    REQUEST_EXTRAS,
    STATED_REPLACEMENT,
    APIChange,
    object_replacements,
    parameter_replacements,
    signature_parameters,
    stated_alternatives,
)
from since_cutoff.errors import SinceCutoffError
from since_cutoff.models import DEFAULT_CUTOFF_MARGIN, compare_date

BLOCK_START = "<!-- since-cutoff:start -->"
BLOCK_END = "<!-- since-cutoff:end -->"
META_START = "<!-- since-cutoff:meta "
META_END = " -->"
BLOCK_VERSION = 2
TITLE = "## Library changes after the model's training cutoff"
REPO_URL = "https://github.com/MohammadHijjawi97/since-cutoff"

# What a note's tags say was checked (see the module docstring), as results.json writes them.
TAG_DIFF = "diff"
TAG_LIBRARY = "library"
TAG_MOVE_CHECKED = "move_checked"
TAG_PROBABLE_RENAME = "probable_rename"
TAG_TYPE_CHECKED = "type_checked"
TAG_NOT_CONFIRMED = "not_confirmed"
TAG_METADATA = "metadata"
TAG_WORDS = {
    TAG_DIFF: "diff",
    TAG_LIBRARY: "library",
    TAG_MOVE_CHECKED: "move checked",
    TAG_TYPE_CHECKED: "type-checked",
    TAG_PROBABLE_RENAME: "probable rename",
    TAG_NOT_CONFIRMED: "not confirmed",
    TAG_METADATA: "metadata",
}
_GUESSES = (TAG_PROBABLE_RENAME, TAG_NOT_CONFIRMED)  # after a "; " in the tag: not evidence
# The header's legend: the first three always (a reader meets them in every block); the others
# only in a block that uses them.
_LEGEND = {
    TAG_DIFF: "[diff] a static comparison of the two releases' public APIs",
    TAG_LIBRARY: (
        "[library] the replacement is named in the library's own deprecation text and exists "
        "in the pinned version"
    ),
    TAG_TYPE_CHECKED: "[type-checked] an example that basedpyright accepts for the pinned version",
    TAG_MOVE_CHECKED: (
        "[move checked] the object at the new path keeps the old one's public names or parameters"
    ),
    TAG_METADATA: (
        "[metadata] the two releases' declared requirements (Requires-Dist in their wheels' "
        "METADATA)"
    ),
    TAG_PROBABLE_RENAME: "[probable rename] a guess from the parameter's position and type",
    TAG_NOT_CONFIRMED: "[not confirmed] names that look similar, not confirmed as replacements",
}
_ALWAYS = (TAG_DIFF, TAG_LIBRARY, TAG_TYPE_CHECKED)

# Where a replacement comes from (Replacement.evidence).
EVIDENCE_LIBRARY = "library_text"
EVIDENCE_MOVE = "move_checked"
EVIDENCE_RENAME = "probable_rename"
EVIDENCE_METADATA = "requires_dist"  # a switched dependency (DEPENDENCY_SWITCHED)

# Who wrote a note (Note.source); a ``run --compare`` baseline's notes carry its arm's name.
NOTE_MODEL = "model"
NOTE_DIFF = "diff"

# What a block covers (its meta line's "scope").
SCOPE_USED = "used"  # the changed APIs the project's code uses (scan, sync)
SCOPE_FAILURES = "failures"  # the changes a model got wrong when since-cutoff tested it (run)
SCOPE_IMPORTED = "imported"  # changes in the packages the code imports (sync --scope imported)
# ``sync --scope imported``: APIs noted per changed package the code imports, by default
# (``--per-package``; the meta line's "per_package" records another choice).
IMPORTED_APIS = 5

# The library says outright that nothing replaces it: similar names are then not shown at all.
_NO_REPLACEMENT = re.compile(r"\b(?:without|no)\s+(?:a\s+)?replacement\b", re.IGNORECASE)

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_BACKTICK = re.compile(r"`([^`]+)`")
_COMMON = {
    "import",
    "from",
    "as",
    "None",
    "True",
    "False",
    "self",
    "cls",
    "await",
    "async",
    "def",
    "return",
    "with",
    "for",
    "in",
    "if",
    "else",
    "and",
    "or",
    "not",
    "is",
    "lambda",
    "str",
    "int",
    "float",
    "bool",
    "list",
    "dict",
    "tuple",
    "print",
    "kwargs",
    "args",
}

# The new version's API (a griffe module from apidiff.load_api) that holds a change, or None;
# only needed for a change from a diff made before DIFF_SCHEMA 13 (no library_names).
ApiLookup = Callable[[APIChange], Any]


# -------------------------------------------------------------------- notes
@dataclass(frozen=True)
class Replacement:
    """What a note says to use instead, and the evidence for it (never a mere similar name)."""

    text: str  # the name to use: `stop`, `pkg.new.Thing`
    evidence: str  # EVIDENCE_*
    # Where the evidence is: "huggingface-hub 1.21.0 huggingface_hub/inference/_client.py"
    # (the library's text), "huggingface-hub 2.0.0 API diff: ..." (a checked move, a rename).
    source: str
    replaces: str | None = None  # the parameter or path it stands in for
    exists_in_locked: bool = True  # every replacement shown exists in the pinned version

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "evidence": self.evidence,
            "source": self.source,
            "replaces": self.replaces,
            "exists_in_locked": self.exists_in_locked,
        }


@dataclass
class Note:
    change: APIChange  # the first change it covers (the only one, for a model's note)
    bullet: str  # without its tag
    example: str | None
    # Deprecated alias of ``checks()["example_type_checks"]``: True only for a model's note
    # whose example type-checks. Kept because results.json has always had it.
    verified: bool
    source: str  # NOTE_MODEL, NOTE_DIFF, or a ``run --compare`` baseline's arm
    tags: tuple[str, ...] = ()  # TAG_*; see tag_list
    changes: list[APIChange] = field(default_factory=list)  # all it covers (one API's)
    replacements: list[Replacement] = field(default_factory=list)
    # Names in the new version that look similar to something removed, not confirmed as
    # replacements: shown next to the note, never in it (unless asked for: diff_note).
    not_confirmed: list[str] = field(default_factory=list)
    writer: str | None = None  # the model that wrote a model's note
    # Whether a model's example for the note type-checked: True for a [type-checked] note,
    # False for the note from the diff that replaced a model's note whose example did not,
    # None when no example was checked (a note from the diff in scan). ``verified`` is it.
    example_type_checks: bool | None = None

    @property
    def covered(self) -> list[APIChange]:
        return self.changes or [self.change]

    @property
    def tag_list(self) -> tuple[str, ...]:
        """The note's tags; a note made without any (0.3's, a baseline's) is ``[type-checked]``
        when it is a model's verified note and ``[diff]`` otherwise."""
        if self.tags:
            return self.tags
        return (TAG_TYPE_CHECKED,) if self.verified and self.source == NOTE_MODEL else (TAG_DIFF,)

    @property
    def line(self) -> str:
        """The bullet as the block writes it, with its tag: ``... do not pass it. [diff]``."""
        return f"{_one_line(self.bullet)} {tag_text(self.tag_list)}".strip()

    @property
    def api(self) -> str:
        """How the note names its API: ``Messages.create``, ``huggingface_hub.hf_hub_download``."""
        return api_name(self.change, self.covered)

    def applies_to(self) -> dict[str, str]:
        c = self.change
        return {"package": c.package, "version": c.to_version, "cutoff_version": c.from_version}

    def checks(self) -> dict[str, Any]:
        """What was checked, and what was not (``measured`` is filled in by a run's report)."""
        c = self.change
        compared = f"{c.from_version} vs {c.to_version}, diff schema {DIFF_SCHEMA}"
        return {
            "change": (
                "Requires-Dist of both releases' wheels (METADATA) and a static comparison of "
                f"their public APIs (griffe), {compared}"
                if c.kind == DEPENDENCY_SWITCHED
                else f"static API diff (griffe), {compared}"
            ),
            "replacement": "; ".join(_replacement_check(r, c) for r in self.replacements) or None,
            "example_type_checks": (
                True if TAG_TYPE_CHECKED in self.tag_list else self.example_type_checks
            ),
            "runtime": runtime_check(self.covered),
            "measured": None,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "change_id": self.change.id,
            "package": self.change.package,
            "bullet": self.bullet,
            "example": self.example,
            "verified": self.verified,
            "source": self.source,
            "api": self.change.path,
            "change_ids": [c.id for c in self.covered],
            "tags": list(self.tag_list),
            "tag_text": tag_text(self.tag_list),
            "applies_to": self.applies_to(),
            "checks": self.checks(),
            "replacement": self.replacements[0].to_dict() if self.replacements else None,
            "replacements": [r.to_dict() for r in self.replacements],
            "not_confirmed": list(self.not_confirmed),
            "writer": self.writer,
        }


def tag_legend(tags: Iterable[str]) -> list[str]:
    """What each of ``tags`` means, as the block's header says it (``[diff] a static comparison
    of the two releases' public APIs``), in the header's order."""
    wanted = set(tags)
    return [_LEGEND[t] for t in _LEGEND if t in wanted]


def tag_text(tags: Iterable[str]) -> str:
    """``[diff]``, ``[diff + library]``, ``[diff; probable rename]``, ``[type-checked]``."""
    tags = list(dict.fromkeys(tags))
    evidence = " + ".join(TAG_WORDS[t] for t in tags if t in TAG_WORDS and t not in _GUESSES)
    guesses = [TAG_WORDS[t] for t in tags if t in _GUESSES]
    text = "; ".join([evidence, *guesses] if evidence else guesses)
    return f"[{text}]" if text else ""


_TAG_WORD = "|".join(re.escape(w) for w in sorted(TAG_WORDS.values(), key=len, reverse=True))
_TAG_AT_END = re.compile(rf"\s\[((?:{_TAG_WORD})(?:(?: \+ |; )(?:{_TAG_WORD}))*)\]$")
_WORD_TAGS = {word: tag for tag, word in TAG_WORDS.items()}


def split_tags(line: str) -> tuple[str, tuple[str, ...]]:
    """A block's bullet and the tags at its end: the inverse of :attr:`Note.line`."""
    m = _TAG_AT_END.search(line)
    if m is None:
        return line, ()
    words = re.split(r" \+ |; ", m.group(1))
    return line[: m.start()], tuple(_WORD_TAGS[w] for w in words)


# ---------------------------------------------------------- notes from the diff
def api_name(change: APIChange, others: Iterable[APIChange] = ()) -> str:
    """How a note names the API of ``change``: its class and name for a member
    (``Messages.create``; a constructor is its class, ``Client``), else its shortest public
    path, re-exports included (``huggingface_hub.hf_hub_download``), of it and of ``others``
    (the other changes of the same API). A member of an SDK's beta mirror (APIChange.in_beta)
    keeps its module, so that it cannot be read as the API of the same name outside ``beta``:
    ``anthropic.resources.beta.messages.messages.Messages.create``. A switched dependency is
    ``httpx -> httpx2``."""
    if change.kind == DEPENDENCY_SWITCHED:
        return change.display
    if change.owner:
        member = change.owner if change.name == "__init__" else f"{change.owner}.{change.name}"
        return f"{change.module}.{member}" if change.in_beta else member
    paths: list[str] = list(
        dict.fromkeys(p for c in (change, *others) for p in (c.path, *(c.import_paths or ())) if p)
    )
    return min(paths, key=lambda p: (p.count("."), len(p), p))


def canonical_order(changes: Iterable[APIChange]) -> list[APIChange]:
    """The changes of one API in an order that does not depend on the project's code: by kind
    (removals first), then each parameter's position in the signature it is read from (the
    old one; the new one for a parameter now required), then its name. The notes block lists
    them so, so that code starting or stopping to pass a parameter does not reword it."""

    def key(c: APIChange) -> tuple[int, int, int, str, str]:
        signature = c.new_signature if c.kind == PARAM_REQUIRED else c.old_signature
        names = signature_parameters(signature)
        wanted = (c.parameter or "").lstrip("*")
        at = names.index(wanted) if wanted in names else -1
        return (
            KIND_PRIORITY.get(c.kind, 9),
            0 if at >= 0 else 1,
            at,
            c.parameter or "",
            c.path,
        )

    return sorted(changes, key=key)


def is_stubs(package: str) -> bool:
    """A stubs-only distribution (``pandas-stubs``, ``types-requests``): its API is type
    declarations, which type checkers read; the runtime package may still accept what they
    dropped."""
    name = package.lower()
    return name.endswith("-stubs") or name.startswith("types-")


def diff_notes(
    changes: Iterable[APIChange], lookup: ApiLookup | None = None, *, suggestions: bool = False
) -> list[Note]:
    """:func:`diff_note` for each API of ``changes`` (APIChange.api_key), in the order of the
    first change of each."""
    by_api: dict[str, list[APIChange]] = {}
    for c in changes:
        by_api.setdefault(c.api_key, []).append(c)
    return [diff_note(group, lookup, suggestions=suggestions) for group in by_api.values()]


def diff_note(
    changes: Sequence[APIChange], lookup: ApiLookup | None = None, *, suggestions: bool = False
) -> Note:
    """One bullet for one API and all its changes (APIChange.api_key), from the diff alone.

    The changes are stated in :func:`canonical_order`, whatever order they come in, so that the
    bullet does not depend on which of them the code uses. A replacement is named only when
    the library's own text states it (the module docstring's table), and the tags say which;
    text that only mentions a name as advice ("If you want to force a new download, use
    `force_download=True`") is quoted, and is not a replacement. What has none is said right
    after what it is about: "huggingface-hub's deprecation text says there is no replacement
    for `x`" where the library says so, else "since-cutoff found no replacement in its
    deprecation text" (what was read: docstrings, deprecation decorators, warnings). Names
    that merely look similar go in ``not_confirmed``, never in the bullet, unless
    ``suggestions`` asks for them (tagged ``[not confirmed]``); where the library says there is
    no replacement, they are dropped. ``lookup`` is only used for a change from a diff older
    than DIFF_SCHEMA 13, which did not record the names its library's text gives.

    A stubs package's notes (``pandas-stubs``) say what its declarations no longer have and
    that type checkers reject it, not that the runtime no longer accepts it.

    Package text (deprecation messages) is untrusted: it is quoted through :func:`safe_text`.
    """
    given = list({id(c): c for c in changes}.values())
    if not given:
        raise ValueError("diff_note needs at least one change")
    ordered = canonical_order(given)
    first = ordered[0]
    package = first.package
    stubs = is_stubs(package)
    name = api_name(first, ordered)
    call = f"`{name}()`"
    tags = [TAG_DIFF]
    sentences: list[str] = []
    replacements: list[Replacement] = []
    similar: list[str] = []
    said_what = False  # a sentence names the API already: the next ones say "it"

    def subject() -> str:
        nonlocal said_what
        text = call if not said_what else "It"
        said_what = True
        return text

    for kind in dict.fromkeys(c.kind for c in ordered):
        group = [c for c in ordered if c.kind == kind]
        params = _unique(c.parameter for c in group if c.parameter)
        if kind == DEPENDENCY_SWITCHED:
            for c in group:
                sentences.append(dependency_bullet(c))
                replacements.append(_dependency_replacement(c))
            tags.append(TAG_METADATA)
            said_what = True
        elif kind == MOVED:
            for c in group:
                sentences.append(_moved_sentence(c))
                if c.move_evidence:
                    tags.append(TAG_MOVE_CHECKED)
                    replacements.append(_move_replacement(c))
            said_what = True
        elif kind == REMOVED:
            for c in group:
                if stubs:
                    sentences.append(
                        f"{package} no longer declares `{name}`; type checkers reject it."
                    )
                else:
                    sentences.append(f"`{name}` was removed; do not use it.")
                if c.namesake:
                    sentences.append(f"The `{c.name}` at `{c.namesake}` is a different object.")
                said_what = True
        elif kind == PARAM_REMOVED:
            plain = [p for p in params if not p.startswith("*")]
            if plain:
                pronoun = "it" if len(plain) == 1 else "them"
                if stubs:
                    sentences.append(
                        f"{package} no longer declares {_listed(plain, 'or')} for {call}; type "
                        f"checkers reject {pronoun}."
                    )
                    said_what = True
                elif extras := _extra_request_arguments(group):
                    # An SDK method that sends a request (Stainless-generated clients: anthropic,
                    # openai, ...) takes fields it has no parameter for in extra_body/extra_query:
                    # the parameter left the signature, not necessarily the API. "do not pass
                    # them" made agents drop a field the task needed (the benchmark pilot).
                    sentences.append(
                        f"{subject()} no longer accepts {_listed(plain, 'or')} as "
                        f"{'keyword arguments' if len(plain) > 1 else 'a keyword argument'}. "
                        f"If the API still needs "
                        f"{pronoun}, pass {pronoun} through its {_listed(extras, 'or')} argument."
                    )
                else:
                    sentences.append(
                        f"{subject()} no longer accepts {_listed(plain, 'or')}; do not pass "
                        f"{pronoun}."
                    )
            for p in params:
                if p.startswith("*"):
                    what = "keyword" if p.startswith("**") else "positional"
                    sentences.append(
                        f"{subject()} no longer accepts extra {what} arguments (`{p}`)."
                    )
        elif kind == DEPRECATED:
            if params:
                verb = "is" if len(params) == 1 else "are"
                pronoun = "it" if len(params) == 1 else "them"
                sentences.append(
                    f"{_listed(params, 'and')} {verb} deprecated for {call}; avoid {pronoun} in "
                    "new code."
                )
                said_what = True
            for c in group:
                if c.parameter:
                    continue
                form = f" called as `{_code(c.call_form)}`" if c.call_form else ""
                sentences.append(f"`{name}`{form} is deprecated; avoid it in new code.")
                said_what = True
        elif kind == PARAM_REQUIRED:
            sentences.append(f"{subject()} now requires {_listed(params, 'and')}.")
        elif kind == PARAM_KEYWORD_ONLY:
            sentences.append(f"Pass {_listed(params, 'and')} to {call} by keyword.")
            said_what = True
        elif kind == PARAM_POSITIONAL_ONLY:
            sentences.append(f"Pass {_listed(params, 'and')} to {call} positionally.")
            said_what = True
        elif kind == KIND_CHANGED:
            for c in group:
                kinds = (
                    f"changed from {c.old_kind} to {c.new_kind}"
                    if c.old_kind and c.new_kind
                    else "changed kind"
                )
                sentences.append(f"`{name}` {kinds}; check its new signature before use.")
                said_what = True
        # What to use instead, for what was removed or deprecated: right after it, so that a
        # sentence about what has no replacement cannot be read as about another change.
        if kind in (REMOVED, PARAM_REMOVED, DEPRECATED):
            more = _replacement_sentences(group, name, lookup, replacements, tags, similar)
            sentences += more
            said_what = said_what and not more  # "It" would not be the API after them
    similar = [s for s in similar if s not in {r.text for r in replacements}]
    if suggestions and similar:
        sentences.append(
            f"Similar names in {first.to_version}, not confirmed as replacements: "
            f"{_listed(similar, 'and')}."
        )
        tags.append(TAG_NOT_CONFIRMED)
    return Note(
        first,
        " ".join(_unique(sentences)),
        None,
        False,
        NOTE_DIFF,
        tuple(dict.fromkeys(tags)),
        changes=given,
        replacements=replacements,
        not_confirmed=similar,
    )


def _replacement_sentences(
    group: list[APIChange],
    name: str,
    lookup: ApiLookup | None,
    replacements: list[Replacement],
    tags: list[str],
    similar: list[str],
) -> list[str]:
    """What to use instead of each change of ``group`` (one kind, of one API), and what has
    none; ``replacements``, ``tags`` and ``similar`` gather the evidence for the note."""
    package = group[0].package
    sentences: list[str] = []
    none_said: list[str] = []  # the library's own text says nothing replaces it
    unnamed: list[str] = []  # removed, and no replacement found in the library's text
    items: list[str] = []
    for c in group:
        what = c.parameter or name
        if c.parameter and c.parameter.startswith("*"):
            continue
        items.append(what)
        if says_no_replacement(c):
            none_said.append(what)
            continue
        found = _library_evidence(c, lookup)
        if found is not None and found[1] is not None:
            sentences.append(found[0])
            replacements.append(found[1])
            tags.append(TAG_LIBRARY)
            continue
        if found is not None:
            sentences.append(found[0])  # advice, quoted: not a replacement
        if c.kind == PARAM_REMOVED and c.renamed and c.suggestions:
            new = c.suggestions[0]
            sentences.append(f"`{what}` was probably renamed to `{new}` (same position and type).")
            replacements.append(
                Replacement(
                    new,
                    EVIDENCE_RENAME,
                    f"{c.package} {c.to_version} API diff: `{new}` has the position and "
                    f"type `{what}` had in {c.from_version}",
                    what,
                )
            )
            tags.append(TAG_PROBABLE_RENAME)
            continue
        if c.kind == DEPRECATED:
            if not c.parameter and found is None:
                quote = _quote_text(c.deprecation or "")
                if quote:
                    sentences.append(f'{c.package} {c.to_version} says: "{quote}"')
            continue  # still works: nothing to replace yet
        unnamed.append(what)
        similar += [s for s in c.suggestions if s not in similar]
    if none_said:
        sentences.append(
            f"{package}'s deprecation text says there is no replacement for "
            f"{_listed(_unique(none_said), 'or')}."
        )
    if unnamed:
        # Named when the sentence is not about everything it follows.
        some = _unique(unnamed) != _unique(items)
        rest = f" for {_listed(_unique(unnamed), 'or')}" if some else ""
        sentences.append(
            f"since-cutoff found no replacement{rest} in {package}'s deprecation text."
        )
    return sentences


def says_no_replacement(change: APIChange) -> bool:
    """Does the library's own text for ``change`` say that nothing replaces it ("deprecated
    without replacement")? Read in the old version's deprecation text, the new version's, and
    what the new version's code that still handles a removed parameter says of it
    (APIChange.still_handled_text: huggingface_hub 2.0's ``_validators.py``). Similar names are
    then never shown, and the notes say so instead of quoting other advice."""
    texts = (change.hint, change.deprecation, change.still_handled_text)
    return any(_NO_REPLACEMENT.search(t or "") for t in texts)


def similar_names(change: APIChange) -> list[str]:
    """Names in the new version that merely look like replacements for ``change``: its
    suggestions, unless they are a parameter renamed in place (then a probable rename) or the
    library says there is no replacement. Every report labels them "not confirmed"."""
    if change.renamed or says_no_replacement(change):
        return []
    return list(change.suggestions)


def similar_text(change: APIChange) -> str | None:
    """``similar parameters in 2.0.0, not confirmed as replacements: `x` ``, or None."""
    names = similar_names(change)
    if not names:
        return None
    what = "parameters" if change.kind == PARAM_REMOVED else "names"
    shown = ", ".join(f"`{n}`" for n in names)
    return f"similar {what} in {change.to_version}, not confirmed as replacements: {shown}"


def rename_text(change: APIChange) -> str | None:
    """``probably renamed to `start` (same position and type)`` for a parameter the diff found
    renamed in place, or None."""
    if change.kind != PARAM_REMOVED or not change.renamed or not change.suggestions:
        return None
    return f"probably renamed to `{change.suggestions[0]}` (same position and type)"


def runtime_text(changes: Iterable[APIChange]) -> str | None:
    """The "Runtime:" caveat for removed parameters the new version's source still reads by
    name (APIChange.still_handled_at), or None. It is not written into the notes block: the
    instruction to the assistant ("do not pass it") is the same either way.

    The names in alphabetical order, and where the source reads them: one file's lines as a
    range (``huggingface_hub/utils/_validators.py:178-203``), several files each with its
    lines. Neither depends on the order the changes come in.

    For a switched dependency, where the pinned release's source still names the one it no
    longer requires (:func:`dependency_runtime`)."""
    changes = list(changes)
    switched = [text for c in changes if (text := dependency_runtime(c))]
    if switched:
        return "; ".join(sorted(switched))
    handled = [c for c in changes if c.still_handled_at and c.parameter]
    if not handled:
        return None
    names = sorted(_unique(c.parameter for c in handled if c.parameter))
    them = "it" if len(names) == 1 else "them"
    return (
        f"{handled[0].to_version}'s source still handles {_listed(names, 'and')} "
        f"({_where(c.still_handled_at or '' for c in handled)}), so calls passing {them} may "
        f"run with a warning; type checkers reject {them}."
    )


def _where(places: Iterable[str]) -> str:
    """``a.py:178-203`` for lines 178 to 203 of one file; ``a.py:10, b.py:20`` for several."""
    lines: dict[str, set[int]] = {}
    other: list[str] = []
    for place in places:
        path, _, line = place.rpartition(":")
        if path and line.isdigit():
            lines.setdefault(path, set()).add(int(line))
        else:
            other.append(place)
    parts = []
    for path in sorted(lines):
        low, high = min(lines[path]), max(lines[path])
        parts.append(f"{path}:{low}" if low == high else f"{path}:{low}-{high}")
    return ", ".join([*parts, *sorted(set(other))])


def runtime_check(changes: Iterable[APIChange]) -> str:
    """``checks["runtime"]``: nothing of the library is run, and what its source shows."""
    text = runtime_text(changes)
    return "not checked" if text is None else f"not checked (nothing was run); {text}"


def _library_evidence(
    change: APIChange, lookup: ApiLookup | None
) -> tuple[str, Replacement | None] | None:
    """What the library's own deprecation text for ``change`` gives, when it names something
    that exists in the new version (else None): the sentence, and the replacement when the
    text states it as one ("Use `stop` instead."). Text that only mentions a name as advice
    ("If you want to force a new download, use `force_download=True`") gives a quote and no
    replacement (None), or nothing when there is nothing worth quoting."""
    names = library_names(change, lookup)
    if not names:
        return None
    best = names[0]
    last = best.rsplit(".", 1)[-1]
    # The old version's text (``hint``) or the pinned one's (``deprecation``): the one that
    # names it, and so the version and file the note cites.
    if change.hint and (not change.deprecation or _mentions(change.hint, last)):
        text, version, where, said = change.hint, change.from_version, change.hint_file, "said"
    else:
        text = change.deprecation or ""
        version, where, said = change.to_version, change.deprecation_file, "says"
    source = f"{change.package} {version}" + (f" {where}" if where else "")
    what = change.parameter or api_name(change)
    stated = _stated_replacement(text, names)
    if stated:
        shown = _listed([_shown_name(s) for s in stated], "or")
        sentence = (
            f"Use {shown} instead of `{what}`." if change.parameter else f"Use {shown} instead."
        )
        return sentence, Replacement(stated[0], EVIDENCE_LIBRARY, source, what)
    quote = _quote(text, best)
    if not quote:
        return None
    # "On `x`," only for a parameter: the API itself is what the bullet is about already.
    on = f"On `{what}`, " if change.parameter else ""
    return f'{on}{change.package} {version} {said}: "{quote}"', None


def library_replacement(change: APIChange) -> Replacement | None:
    """The replacement that the library's own deprecation text states for ``change`` and that
    exists in the pinned version (what a note tags ``[diff + library]``), or None: for text
    that only mentions a name as advice, that says nothing replaces it, or for a change of a
    kind that has no replacement to name (a move, a parameter now required)."""
    if change.kind not in (REMOVED, PARAM_REMOVED, DEPRECATED) or says_no_replacement(change):
        return None
    found = _library_evidence(change, None)
    return found[1] if found is not None else None


def _shown_name(path: str) -> str:
    """How a note names a replacement: a member of a class as ``Class.name``
    (``BaseChatModel.invoke``, as the note names its API), anything else by its full path."""
    parts = path.split(".")
    if len(parts) >= 3 and parts[-2][:1].isupper():
        return ".".join(parts[-2:])
    return path


def library_names(change: APIChange, lookup: ApiLookup | None = None) -> list[str]:
    """APIChange.library_names; for a change from a diff older than DIFF_SCHEMA 13, the
    parameters of ``new_signature`` (as far as it goes) and, with ``lookup``, the functions and
    classes of the new API that the text names."""
    if change.library_names is not None:
        return list(change.library_names)
    text = " ".join(t for t in (change.hint, change.deprecation) if t)
    if not text:
        return []
    found: list[str] = []
    if change.parameter:
        found += parameter_replacements(
            text, signature_parameters(change.new_signature), change.parameter
        )
    root = lookup(change) if lookup is not None else None
    if root is not None:
        found += object_replacements(change, text, root)
    return list(dict.fromkeys(found))


def _stated_replacement(text: str, names: list[str]) -> list[str] | None:
    """The ones of ``names`` that ``text`` states as the replacement, the first it gives
    first ("Use `stop` instead."; httpx 0.27's "Use 'proxy' or 'mounts' instead."), or None
    when the text only mentions them ("If you want to force a new download, use
    `force_download=True`" is advice, not a replacement)."""
    for pattern in STATED_REPLACEMENT:
        for m in re.finditer(pattern, text, re.IGNORECASE):
            said = [_named_as(s, names) for s in (m.group("n").strip("."), *stated_alternatives(m))]
            if said[0] is not None:
                return list(dict.fromkeys(n for n in said if n is not None))
    return None


def _named_as(said: str, names: list[str]) -> str | None:
    """The one of ``names`` that ``said`` names: itself, or by its last parts."""
    return next(
        (n for n in names if n == said or n.endswith("." + said) or said.endswith("." + n)), None
    )


# A sentence of a deprecation message that only says what the bullet says already ("X is
# deprecated and will be removed in a future version."), or asks the reader to get in touch.
_BOILERPLATE = re.compile(r"deprecat|will be removed|scheduled for removal|removed in", re.I)
_ADVICE = re.compile(r"\b(?:use|instead|replac\w*|favou?r|prefer|migrat\w*|switch)\b", re.I)
_NOISE = re.compile(
    r"https?://|\b(?:open|file|report|raise)\s+(?:an?\s+)?(?:issue|bug)|let us know", re.I
)


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", " ".join(text.split())) if s]


def _worth_quoting(sentence: str) -> bool:
    """Not boilerplate the bullet says already, and not a request to open an issue."""
    if _NOISE.search(sentence):
        return False
    return not (_BOILERPLATE.search(sentence) and not _ADVICE.search(sentence))


def _whole_sentences(sentences: list[str], limit: int) -> str:
    """As many of ``sentences`` as fit in ``limit`` characters, each whole (:func:`safe_text`
    made): "" when not even the first one fits. A quote never stops mid-sentence."""
    out: list[str] = []
    for s in sentences:
        text = safe_text(" ".join([*out, s]), 10**6)
        if len(text) > limit:
            break
        out.append(s)
    return safe_text(" ".join(out), 10**6) if out else ""


def _quote(text: str, name: str, limit: int = 160) -> str:
    """What ``text`` says up to the sentence that names ``name``, safe to quote
    (:func:`safe_text`), with ``name`` (and ``name=value``) in a code span again: it exists in
    the pinned version.

    Sentences that only say "deprecated" (the bullet says as much) or ask to open an issue are
    left out, and so is anything before the naming sentence when the quote would be longer
    than ``limit``. Only whole sentences are quoted: "" when the naming one alone is longer.
    """
    last = name.rsplit(".", 1)[-1]
    sentences = _sentences(text)
    at = next((i for i, s in enumerate(sentences) if _mentions(s, last)), None)
    if at is None:
        chosen = [s for s in sentences if _worth_quoting(s)]
    else:
        chosen = [s for s in sentences[:at] if _worth_quoting(s)] + [sentences[at]]
        while len(chosen) > 1 and len(safe_text(" ".join(chosen), 10**6)) > limit:
            chosen.pop(0)
    quote = _whole_sentences(chosen, limit)
    return re.sub(rf"'((?:[\w.]*\.)?{re.escape(last)}(?:=[^'\s]*)?)'", r"`\1`", quote)


def _quote_text(text: str, limit: int = 160) -> str:
    """A deprecation message worth quoting in a note: its sentences that say more than
    "deprecated", each whole, within ``limit`` characters; "" when none does."""
    return _whole_sentences([s for s in _sentences(text) if _worth_quoting(s)], limit)


def _mentions(text: str, name: str) -> bool:
    return re.search(rf"(?<![\w]){re.escape(name)}(?!\w)", text) is not None


def _moved_sentence(change: APIChange) -> str:
    """``\\`pkg.a.Thing\\` moved to \\`pkg.b.Thing\\`: import it with \\`from pkg.b import
    Thing\\`.``; for a whole package (selection.package_moves), "The package `pkg.a` moved to
    `pkg.b`; import from there."; for an object that came back under another name
    (APIChange.renamed_to), "`Old` is now `New`: ..." first."""
    target = change.moved_to or ""
    if change.is_package_move:
        return f"The package `{change.path}` moved to `{target}`; import from there."
    module, _, name = target.rpartition(".")
    how = f"import it with `from {module} import {name}`" if module else ""
    renamed = change.renamed_to
    if renamed:
        tail = f"; {how}" if how else ""
        return f"`{change.name}` is now `{renamed}`: `{change.path}` moved to `{target}`{tail}."
    tail = f": {how}" if how else ""
    return f"`{change.path}` moved to `{target}`{tail}."


def _move_replacement(change: APIChange) -> Replacement:
    evidence = change.move_evidence or {}
    compared, kept, of = evidence.get("compared"), evidence.get("kept"), evidence.get("of")
    diff = f"{change.package} {change.to_version} API diff"
    if change.is_package_move:
        modules = int(evidence.get("modules") or 0)
        others = int(of or 0) - modules
        counted = f"{modules} modules" + (f" and {others} other objects" if others > 0 else "")
        return Replacement(
            change.moved_to or "",
            EVIDENCE_MOVE,
            f"{diff}: the {counted} of `{change.path}` are at `{change.moved_to}`, each "
            "keeping the old one's public names or parameters",
            change.path,
        )
    what = {
        "class": "public names",
        "module": "public names",
        "function": "public parameters",
    }.get(str(compared), "")
    counted = f"keeps {kept} of {of} {what}" if what and of else f"is the same {compared}"
    renamed = (
        f", and no `{change.renamed_to}` existed in {change.from_version}"
        if change.renamed_to
        else ""
    )
    return Replacement(
        change.moved_to or "",
        EVIDENCE_MOVE,
        f"{diff}: the object at `{change.moved_to}` {counted} of the old one{renamed}",
        change.path,
    )


# The longest a switched dependency's note may be, its tag included: a note is one or two
# sentences. The counts and the other places are in dependency_detail (scan --all, report.md,
# the MCP tools) and in the JSON.
DEPENDENCY_NOTE_LIMIT = 300
# At most this many places in the note; in dependency_detail this many examples, then
# re-exports, then pairs of base classes.
DEPENDENCY_NOTE_FACTS = 3
DEPENDENCY_EXAMPLES = 6
_DEPENDENCY_REEXPORTS = 3
_DEPENDENCY_BASE_PAIRS = 3


def dependency_bullet(change: APIChange) -> str:
    """The note for a DEPENDENCY_SWITCHED change, without its tag: what the pinned release
    requires instead (APIChange.describe), a few places of its public API that name the new
    distribution's types where they named the old one's (the signatures code most likely calls
    first, a base class, a re-export), and what to use there. At most
    :data:`DEPENDENCY_NOTE_LIMIT` characters with its tag: a place that would not fit is left
    out, and the counts are in :func:`dependency_detail`. Only what both releases' metadata
    and public APIs show: whether the pinned release still accepts the old distribution's
    objects at run time differs between libraries and is not claimed (the "Runtime:" line of
    the reports points to where its source still names it)."""
    d = change.dependency or {}
    head = change.describe()
    close = _dependency_close(change)
    budget = DEPENDENCY_NOTE_LIMIT - len(" " + tag_text((TAG_DIFF, TAG_METADATA)))
    facts: list[str] = []
    for fact in _note_facts(d):
        fits = len(_joined(head, [*facts, fact], close)) <= budget
        if fits and len(facts) < DEPENDENCY_NOTE_FACTS:
            facts.append(fact)
    text = _joined(head, facts, close)
    if len(text) <= budget:
        return text
    return f"{head}." if len(head) < budget else head[: budget - 4] + "..."


def _joined(head: str, facts: Sequence[str], close: str) -> str:
    return f"{head}: {_listed_plain(facts)}. {close}" if facts else f"{head}. {close}"


def _dependency_close(change: APIChange) -> str:
    """``Use `httpx2` there, not `httpx`.``; for a copy the package ships, its own names
    (``Use typer's own names there (`typer.BadParameter`, `typer.Context`), not `click`'s.``)."""
    d = change.dependency or {}
    old, new = change.name, change.switched_to or "?"
    if not d.get("vendored"):
        return f"Use `{new}` there, not `{old}`."
    names = [f"`{_code(str(n))}`" for n in d.get("public_names") or ()][:3]
    if names:
        return f"Use {change.package}'s own names there ({', '.join(names)}), not `{old}`'s."
    return f"Use `{_code(new)}`'s types there, not `{old}`'s."


def _note_facts(d: dict[str, Any]) -> list[str]:
    """The note's places, in the order they are tried: the first two examples (a parameter
    with its async twin's type when that differs: ``(`httpx2.AsyncClient` for
    `AsyncOpenAI`)``), a base class, the next example, a re-export."""
    examples = _merged_examples(d.get("examples") or [])
    bases = _base_clauses(d.get("bases") or [], limit=2)
    reexports = [_reexport_clause([r]) for r in list(d.get("reexports") or [])[:1]]
    return [*examples[:2], *bases[:1], *examples[2:3], *reexports]


def _merged_examples(examples: Sequence[dict[str, Any]]) -> list[str]:
    """One short text per example (:func:`_example_text` without counts). An example whose
    twin (the same parameter or return of the async or sync counterpart: ``AsyncOpenAI`` for
    ``OpenAI``, ``set_async_client_factory`` for ``set_client_factory``) came first is left
    out, and its type added to the twin's when it differs."""
    out: list[tuple[dict[str, Any], list[str]]] = []  # (first example, the twins' other types)
    seen: dict[tuple[str, str, str], int] = {}
    for e in examples:
        key = (
            str(e.get("context")),
            _twin_name(str(e.get("display"))),
            _twin_name(str(e.get("parameter") or "")),
        )
        if key in seen:
            first, others = out[seen[key]]
            new = f"`{_code(str(e.get('new')))}`"
            if new != f"`{_code(str(first.get('new')))}`" and not any(new in o for o in others):
                others.append(f"{new} for `{_code(str(e.get('display')))}`")
            continue
        seen[key] = len(out)
        out.append((e, []))
    texts = []
    for first, others in out:
        text = _example_text(first, None)
        texts.append(f"{text} ({'; '.join(others)})" if others else text)
    return texts


def _twin_name(name: str) -> str:
    return re.sub(r"async_?|aio_?", "", name.lower()).replace("_", "")


def dependency_detail(change: APIChange) -> str:
    """What the note leaves out, for scan --all, report.md and the MCP tools: ``its
    Requires-Dist lists `httpx2<3,>=2.12.0` and no `httpx`; 655 places in its public API that
    named `httpx` types name the `httpx2` types of the same name: `OpenAI(http_client=...)`
    (and 7 other signatures) takes `httpx2.Client`, ...; `openai.DefaultHttpxClient` ... are
    now ...``: the requirements (:func:`dependency_requires`), how many places switched, up
    to :data:`DEPENDENCY_EXAMPLES` of them (a parameter with how many other signatures name
    the same type there, a return, an attribute), then re-exports, then base classes (only a
    class whose base itself is the other distribution's "derives from" it; a type argument of
    a base is a place, not a base), as many as :data:`_DEPENDENCY_BASE_PAIRS` pairs of types
    and then how many more."""
    d = change.dependency or {}
    old, new = change.name, change.switched_to or "?"
    sites, same = int(d.get("sites") or 0), int(d.get("sites_same_name") or 0)
    types = f"the `{new}` types of the same name" if sites and same == sites else f"`{new}` types"
    places = "place" if sites == 1 else "places"
    verb = "names" if sites == 1 else "name"
    text = f"{sites} {places} in its public API that named `{old}` types {verb} {types}"
    counts = _type_counts(d)
    examples = list(d.get("examples") or [])[:DEPENDENCY_EXAMPLES]
    clauses = [_listed_plain([_example_text(e, counts) for e in examples])]
    reexports = list(d.get("reexports") or [])[:_DEPENDENCY_REEXPORTS]
    if reexports:
        clauses.append(_reexport_clause(reexports))
    bases = list(d.get("bases") or ())
    pairs = _base_clauses(bases, limit=3)
    clauses += pairs[:_DEPENDENCY_BASE_PAIRS]
    kinds = [(str(b.get("old")), str(b.get("new"))) for b in bases]
    shown = set(list(dict.fromkeys(kinds))[:_DEPENDENCY_BASE_PAIRS])
    rest = int(d.get("bases_count") or len(bases)) - sum(k in shown for k in kinds)
    if rest > 0:
        many = rest > 1
        clauses.append(
            f"{rest} more class{'es' if many else ''} derive{'' if many else 's'} from `{new}` "
            f"types instead of `{old}` types"
        )
    clauses = [c for c in clauses if c]
    detail = f"{text}: {'; '.join(clauses)}" if clauses else text
    return f"{dependency_requires(change)}; {detail}"


def dependency_requires(change: APIChange) -> str:
    """``its Requires-Dist lists `httpx2<3,>=2.12.0` and no `httpx```: what the pinned
    release's Requires-Dist says, for the extras the switch concerns (``... and no `httpx` for
    them``), with a new requirement the older release already had (``which 3.45.2 also
    required``) or the extras that still list the old one, or the copy the package ships."""
    d = change.dependency or {}
    old, new = change.name, change.switched_to or "?"
    extras = list(d.get("extras") or ())
    for_them = (" for them" if len(extras) > 1 else " for it") if extras else ""
    if d.get("vendored"):
        return f"its Requires-Dist lists no `{old}`{for_them}, and it ships `{_code(new)}`"
    requirement = f"`{_code(str(d.get('new_requirement') or new))}`"
    also = "" if d.get("new_added", True) else f", which {change.from_version} also required,"
    still = [str(e) for e in d.get("old_in_extras") or ()]
    if still:
        which = f"{_listed(still, 'and')} extra{'s' if len(still) > 1 else ''}"
        return f"its Requires-Dist lists {requirement}{also.rstrip(',')}, and `{old}` only for its {which}"
    return f"its Requires-Dist lists {requirement}{also} and no `{old}`{for_them}"


def _type_counts(d: dict[str, Any]) -> dict[tuple[str, str, bool], int]:
    """How many signatures name each type at each switched parameter: ``("http_client",
    "httpx2.Client", True)`` -> 8 (``parameter_types``, DIFF_SCHEMA 19)."""
    out: dict[tuple[str, str, bool], int] = {}
    for parameter, kinds in (d.get("parameter_types") or {}).items():
        for k in kinds or ():
            key = (str(parameter), str(k.get("new")), bool(k.get("direct")))
            out[key] = int(k.get("count") or 0)
    return out


def _reexport_clause(reexports: Sequence[dict[str, Any]]) -> str:
    paths = [f"`{_code(r.get('path'))}`" for r in reexports]
    targets = [f"`{_code(r.get('new'))}`" for r in reexports]
    verb = "is" if len(reexports) == 1 else "are"
    return f"{_listed_plain(paths)} {verb} now {_listed_plain(targets)}"


def _base_clauses(bases: Iterable[dict[str, Any]], limit: int) -> list[str]:
    """```A` and `B` derive from `httpx2.Auth` instead of `httpx.Auth```: one per pair of
    types, at most ``limit`` classes each, then "others"."""
    by_pair: dict[tuple[str, str], list[str]] = {}
    for b in bases:
        by_pair.setdefault((str(b.get("old")), str(b.get("new"))), []).append(
            f"`{_code(b.get('display'))}`"
        )
    out = []
    for (was, now), names in by_pair.items():
        shown = names[:limit] + (["others"] if len(names) > limit else [])
        verb = "derives" if len(shown) == 1 else "derive"
        out.append(f"{_listed_plain(shown)} {verb} from `{_code(now)}` instead of `{_code(was)}`")
    return out


def _example_text(example: dict[str, Any], counts: dict[tuple[str, str, bool], int] | None) -> str:
    """```OpenAI(http_client=...)` (and 7 other signatures) takes `httpx2.Client```: one place
    and what it names now, with (``counts``) how many other signatures name that same type at
    that parameter. "Takes" / "returns" / "is" only for a type that is the annotation or a
    member of its union; otherwise the annotation "names" it (``Callable[[],
    httpx2.Client]``)."""
    display = _code(example.get("display"))
    new = f"`{_code(example.get('new'))}`"
    direct = bool(example.get("direct"))
    context = example.get("context")
    if context == "param":
        parameter = _code(example.get("parameter"))
        key = (str(example.get("parameter")), str(example.get("new")), direct)
        others = (counts or {}).get(key, 1) - 1
        more = f" (and {others} other signature{'s' if others > 1 else ''})" if others > 0 else ""
        what = f"takes {new}" if direct else f"has {new} in its annotation"
        return f"`{display}({parameter}=...)`{more} {what}"
    if context == "return":
        return (
            f"`{display}()` returns {new}" if direct else f"`{display}()`'s annotation names {new}"
        )
    return f"`{display}` is {new}" if direct else f"`{display}`'s annotation names {new}"


def _listed_plain(items: Sequence[str]) -> str:
    """``a``, ``a and b``, ``a, b and c`` (items as they are)."""
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} and {items[-1]}"


def _dependency_replacement(change: APIChange) -> Replacement:
    d = change.dependency or {}
    new = change.switched_to or "?"
    old = f"`{_code(str(d.get('old_requirement') or change.name))}`"
    if d.get("vendored"):
        requires = f"no longer lists {old}, and the package ships `{_code(new)}`"
    else:
        requires = (
            f"lists `{_code(str(d.get('new_requirement') or new))}` and "
            f"{change.from_version}'s listed {old}"
        )
    sites = int(d.get("sites") or 0)
    return Replacement(
        new,
        EVIDENCE_METADATA,
        f"{change.package} {change.to_version} Requires-Dist {requires}; the API diff found "
        f"{sites} place{'' if sites == 1 else 's'} that named `{change.name}` types naming "
        f"`{new}` types",
        change.name,
    )


def dependency_runtime(change: APIChange) -> str | None:
    """Where the pinned release's source still names the distribution it no longer requires
    (APIChange.dependency's ``still_named_at``), for the "Runtime:" line; None when it does not.
    What that code does with the old distribution's objects (openai 3 converts them, anthropic
    1.8 raises TypeError, according to their sources) differs, and nothing was run to tell."""
    d = change.dependency or {}
    places = [str(p) for p in d.get("still_named_at") or ()]
    if change.kind != DEPENDENCY_SWITCHED or not places:
        return None
    total = int(d.get("still_named_count") or len(places))
    more = f" and {total - len(places)} more" if total > len(places) else ""
    return (
        f"{change.to_version}'s source still names `{change.name}` ({_lines(places)}{more}); "
        f"what it does with `{change.name}` objects was not checked"
    )


def _lines(places: Iterable[str]) -> str:
    """``a.py:217, 223, 238, b.py:10``: each file once, with its lines."""
    lines: dict[str, list[str]] = {}
    for place in places:
        path, _, line = place.rpartition(":")
        lines.setdefault(path or place, []).append(line)
    return ", ".join(f"{path}:{', '.join(found)}" for path, found in lines.items())


def _replacement_check(r: Replacement, change: APIChange) -> str:
    if r.evidence == EVIDENCE_LIBRARY:
        return (
            f"`{r.text}` is named in the library's deprecation text ({r.source}) and exists in "
            f"{change.package} {change.to_version}"
        )
    if r.evidence == EVIDENCE_RENAME:
        return f"a guess: {r.source}"
    return r.source


def _unique(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _extra_request_arguments(changes: Sequence[APIChange]) -> list[str]:
    """``extra_body`` / ``extra_query`` when the pinned callable of these changes takes them.

    From ``request_extras`` (DIFF_SCHEMA 17), else from the recorded new signature, which
    is cut at 400 characters. Whether the API itself still takes a removed field is not
    something a static diff can know, so the note says "if the API still needs".
    """
    found: set[str] = set()
    for c in changes:
        if c.request_extras is not None:
            found.update(c.request_extras)
        else:
            signature = c.new_signature or ""
            found.update(
                name for name in REQUEST_EXTRAS if re.search(rf"[(,]\s*{name}\s*[:=,)]", signature)
            )
    return [name for name in REQUEST_EXTRAS if name in found]


def _listed(names: Sequence[str], conjunction: str) -> str:
    """```a```, ```a` or `b```, ```a`, `b` or `c``` (``names`` as code)."""
    spans = [f"`{n}`" for n in names]
    if len(spans) <= 1:
        return "".join(spans)
    return f"{', '.join(spans[:-1])} {conjunction} {spans[-1]}"


def _code(text: str | None) -> str:
    """Text for a Markdown code span: one line, no backtick that would end the span."""
    return " ".join((text or "").split()).replace("`", "'")


def _one_line(text: str) -> str:
    return " ".join(text.replace("<!--", "").replace("-->", "").split())


# ------------------------------------------------------------ model's notes
def template_bullet(change: APIChange) -> str:
    """A factual bullet derived only from the API diff (never wrong, sometimes less helpful).

    The ``--compare template`` baseline: its text stays as 0.3 wrote it, so the numbers
    measured against it stay comparable. The notes since-cutoff writes are :func:`diff_note`.
    """
    pkg = f"{change.package} {change.to_version}"
    # Only the text 0.3 read (a deprecation decorator, a docstring line naming it), so that the
    # ``--compare template`` baseline, and the numbers measured against it, stay the same. A
    # removed parameter's own docstring entry and warnings.warn text (DIFF_SCHEMA 12) are not.
    old = change.hint_source not in (HINT_PARAM_DOC, HINT_WARNING)
    hint = f" ({safe_text(change.hint).rstrip('.')})" if change.hint and old else ""
    if change.kind == MOVED and change.moved_to:
        module, _, name = change.moved_to.rpartition(".")
        return f"`{change.path}` moved: use `from {module} import {name}` ({pkg})."
    if change.kind == REMOVED:
        other = (
            f" The `{change.name}` at `{change.namesake}` is a different object."
            if change.namesake
            else ""
        )
        return f"`{change.display}` no longer exists in {pkg}{hint}. Do not use it.{other}"
    if change.kind == PARAM_REMOVED:
        return (
            f"`{change.display}`: `{change.parameter}` was removed in {pkg}{hint}. Do not pass it."
        )
    if change.kind == PARAM_REQUIRED:
        return f"`{change.display}` now requires `{change.parameter}` in {pkg}."
    if change.kind == PARAM_KEYWORD_ONLY:
        return f"`{change.owner + '.' if change.owner else ''}{change.name}`: pass `{change.parameter}` by keyword in {pkg}."
    if change.kind == PARAM_POSITIONAL_ONLY:
        return f"`{change.owner + '.' if change.owner else ''}{change.name}`: pass `{change.parameter}` positionally in {pkg}."
    if change.kind == DEPRECATED:
        text = safe_text(change.deprecation or "").rstrip(".")
        msg = (
            f": {text}" if text and text.lower() not in ("deprecated", "deprecated method") else ""
        )
        if change.parameter:
            return (
                f"`{change.display}`: `{change.parameter}` is deprecated in {pkg}{msg}. "
                "Avoid it in new code."
            )
        return f"`{change.path}` is deprecated in {pkg}{msg}. Avoid it in new code."
    if change.kind == KIND_CHANGED:
        kinds = (
            f"changed from {change.old_kind} to {change.new_kind}"
            if change.old_kind and change.new_kind
            else "changed kind"
        )
        return f"`{change.path}` {kinds} in {pkg}; check its new signature before use."
    return change.describe()


def clean_bullet(text: str) -> str:
    text = " ".join(text.strip().split())
    return text[2:].strip() if text.startswith(("- ", "* ")) else text


def bullet_is_grounded(bullet: str, example: str, change: APIChange) -> bool:
    """Every identifier the bullet puts in backticks must be backed by the example or the diff."""
    allowed = set(_IDENT.findall(example))
    for text in (
        change.path,
        change.display,
        change.parameter or "",
        change.moved_to or "",
        change.old_signature or "",
        change.new_signature or "",
        change.package,
    ):
        allowed.update(_IDENT.findall(text))
    allowed.update(_IDENT.findall(change.package.replace("-", "_")))
    allowed |= _COMMON
    for span in _BACKTICK.findall(bullet):
        # Ignore string literals inside the span: model names, file names, etc.
        code = re.sub(r"([\"']).*?\1", "''", span)
        if not api_identifiers(code) <= allowed:
            return False
    return True


def api_identifiers(code: str) -> set[str]:
    """Identifiers in a code span that name library API: attributes, keywords, calls, classes.

    Plain lower-case variable names (``client`` in ``client.shutdown()``) are placeholders the
    bullet may choose freely, so they are not required to appear in the example.
    """
    found = set(re.findall(r"\.\s*([A-Za-z_]\w*)", code))
    found |= set(re.findall(r"([A-Za-z_]\w*)\s*=(?!=)", code))
    found |= set(re.findall(r"(?<![\w.])([A-Za-z_]\w*)\s*\(", code))
    found |= set(re.findall(r"(?<![\w.])([A-Z]\w*)", code))
    found |= set(re.findall(r"(?:from|import)\s+([\w.]+)", code))
    expanded: set[str] = set()
    for f in found:
        expanded.update(_IDENT.findall(f))
    return expanded


def safe_text(text: str, limit: int = 160) -> str:
    """Package-provided text (docstrings, deprecation messages) is untrusted: keep it short,
    single-line, and unable to close the block or smuggle code spans."""
    text = " ".join(text.split())
    text = text.replace("<!--", "").replace("-->", "").replace("`", "'")
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


# ------------------------------------------------------------------ the block
def deps_hash(pairs: Iterable[tuple[str, str]]) -> str:
    """The meta line's ``deps``: a hash of the sorted ``name==version`` of every dependency
    scanned, so that a changed lockfile shows."""
    text = "\n".join(sorted({f"{name}=={version}" for name, version in pairs}))
    return _hash(text)


def body_hash(body: str) -> str:
    """The meta line's ``body``: a hash of the block's lines after the meta line (up to the end
    marker), with LF line breaks, so that a hand edit shows."""
    return _hash(body.replace("\r\n", "\n"))


def _hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def render_block(
    notes: Iterable[Note],
    *,
    model: str,
    cutoff: date,
    version_source: str,
    deps: str | None = None,
    scope: str = SCOPE_USED,
    tool: str = __version__,
    suggestions: bool = False,
    per_package: int | None = None,
    margin: int = DEFAULT_CUTOFF_MARGIN,
) -> str:
    """The notes block (format 2) for AGENTS.md / CLAUDE.md.

    ``deps`` is :func:`deps_hash` of every dependency scanned (by default, of the packages and
    versions the notes are about); ``scope`` what the notes cover (SCOPE_*); ``margin`` how many
    days before the cutoff the comparison releases were published by (``--cutoff-margin``; the
    header says the day, and the meta line keeps the number, so that ``status`` sees a block
    compared from another day). The first line is
    0.3's marker, so 0.3's ``unapply`` still finds and removes a block written by 0.4.
    ``suggestions`` (``sync --suggestions``: the notes name similar names, ``[not confirmed]``)
    is recorded in the meta line, only when set, so that the next ``sync`` keeps it; so is
    ``per_package`` (``sync --scope imported --per-package N``), only when it is not the
    default (:data:`IMPORTED_APIS`) and the scope is SCOPE_IMPORTED.

    Packages are in alphabetical order, and each package's bullets by the API they are about
    (a switched dependency first), so that the block does not depend on the order the notes
    come in (which of them the code uses first). The meta line's ``checked`` says which API each ``[type-checked]`` bullet is
    about (:func:`api_id`, in the order of the bullets), so that ``sync`` keeps one only while
    the code still uses its API.
    """
    notes = list(notes)
    by_pkg: dict[tuple[str, str], list[Note]] = {}
    for n in notes:
        by_pkg.setdefault((n.change.package, n.change.to_version), []).append(n)
    used_tags = {t for n in notes for t in n.tag_list}
    body = [TITLE, "", _header(model, cutoff, version_source, scope, tool, used_tags, margin)]
    checked: list[str] = []
    for (pkg, version), items in sorted(by_pkg.items()):
        olds = _unique(n.change.from_version for n in items if n.change.from_version)
        # The release the notes compare from: the latest published ``margin`` days before the
        # cutoff, which the header says (a block from before the margin said "at the cutoff").
        compared = f" (compared from {olds[0]})" if len(olds) == 1 else ""
        body += ["", f"**{pkg} {version}**{compared}"]
        seen: set[str] = set()
        # A switched dependency first: it concerns every object of it the code hands over.
        for n in sorted(items, key=lambda n: (n.change.kind != DEPENDENCY_SWITCHED, n.api, n.line)):
            line = n.line
            if not _one_line(n.bullet) or line in seen:
                continue
            seen.add(line)
            body.append(f"- {line}")
            if TAG_TYPE_CHECKED in n.tag_list:
                checked.append(api_id(n.change))
    text = "\n".join(body) + "\n"
    if deps is None:
        deps = deps_hash((n.change.package, n.change.to_version) for n in notes)
    meta = {
        "v": BLOCK_VERSION,
        "tool": tool,
        "model": model or None,
        "cutoff": cutoff.isoformat(),
        "margin": margin,
        "versions_from": version_source,
        "deps": deps,
        "body": body_hash(text),
        "scope": scope,
    }
    if suggestions:
        meta["suggestions"] = True
    if scope == SCOPE_IMPORTED and per_package and per_package != IMPORTED_APIS:
        meta["per_package"] = per_package
    if checked:
        meta["checked"] = checked
    return f"{BLOCK_START}\n{_meta_line(meta)}\n{text}{BLOCK_END}\n"


def api_id(change: APIChange) -> str:
    """A short, stable name for the API of ``change`` (a hash of APIChange.api_key), as the
    meta line records it for each ``[type-checked]`` bullet."""
    return hashlib.sha256(change.api_key.encode("utf-8")).hexdigest()[:12]


def model_names(model: str | None) -> list[str]:
    """The models a block is written for: ``sync --model a,b`` writes them as ``"a, b"`` (the
    notes then use the earliest of their cutoffs)."""
    return [m.strip() for m in (model or "").split(",") if m.strip()]


def _header(
    model: str,
    cutoff: date,
    version_source: str,
    scope: str,
    tool: str,
    tags: set[str],
    margin: int = DEFAULT_CUTOFF_MARGIN,
) -> str:
    # The model's name comes from the user or a settings file: it must not end the block either.
    name = _one_line(_code(model))
    names = model_names(name)
    day = cutoff.isoformat()
    # As the model line of ``scan`` says it: "training cutoff 2025-07-31, comparing from
    # releases up to 2025-07-01" (--cutoff-margin 30).
    from_day = (
        f", comparing from releases up to {compare_date(cutoff, margin).isoformat()}"
        if margin > 0
        else ""
    )
    if len(names) > 1:
        who = f"the earliest training cutoff of {_listed(names, 'and')} ({day}{from_day})"
    elif name:
        who = f"the training cutoff of `{name}` ({day}{from_day})"
    else:
        who = f"{day} ({from_day[2:]})" if from_day else day
    source = _one_line(_code(version_source))
    versions = (
        "the latest versions on PyPI"
        if source.startswith("latest")
        else f"the versions in {source_text(source)}"
    )
    if scope == SCOPE_FAILURES:
        what = (
            f"Changed after {who} in this project's dependencies at {versions}, and written "
            f"wrong by {f'`{name}`' if name else 'the model'} when tested"
        )
    elif scope == SCOPE_IMPORTED:
        what = f"Changed after {who} in the packages this project imports, at {versions}"
    else:
        what = f"Changed after {who} and used by this project at {versions}"
    legend = [_LEGEND[t] for t in _LEGEND if t in _ALWAYS or t in tags]
    return (
        f"{what}, according to [since-cutoff]({REPO_URL}) {_one_line(tool)}. Tags: "
        f"{'; '.join(legend)}. "
        "No library code was run. Where these lines conflict with what you remember, follow "
        "these lines."
    )


def source_text(version_source: str) -> str:
    """Where the versions come from, as the header says it: each file in a code span, the rest
    in words. ``pyproject.toml, latest on PyPI for 3 unpinned`` reads
    ```pyproject.toml`, latest on PyPI for 3 unpinned``; ``poetry.lock, requirements.txt for 3
    not in it`` reads ```poetry.lock`, `requirements.txt` for 3 not in it``."""
    parts = []
    for part in _code(version_source).split(", "):
        head, sep, rest = part.partition(" ")
        if head and not part.startswith(("latest", "the ")) and ("." in head or "/" in head):
            parts.append(f"`{head}`{sep}{rest}")
        else:
            parts.append(part)
    return ", ".join(parts)


def _meta_line(meta: dict[str, Any]) -> str:
    # Compact JSON; "<" and ">" escaped so that nothing in it can end the HTML comment.
    text = json.dumps(meta, separators=(",", ":"), ensure_ascii=False)
    text = text.replace("<", "\\u003c").replace(">", "\\u003e")
    return f"{META_START}{text}{META_END}"


def render_legacy_block(
    notes: Iterable[Note], *, model: str, cutoff: date, version_source: str
) -> str:
    """The block as 0.3 wrote it (no meta line, no tags), for the ``run --compare`` baselines:
    their blocks stay byte-identical to 0.3's, so the numbers measured with them stay
    comparable."""
    by_pkg: dict[tuple[str, str], list[Note]] = {}
    for n in notes:
        by_pkg.setdefault((n.change.package, n.change.to_version), []).append(n)
    lines = [
        BLOCK_START,
        TITLE,
        "",
        f"Generated by [since-cutoff]({REPO_URL}) for "
        f"`{model}` (training cutoff {cutoff.isoformat()}), checked against `{version_source}`. "
        "Prefer these over what you remember.",
    ]
    for (pkg, version), items in sorted(by_pkg.items()):
        lines += ["", f"**{pkg} {version}**"]
        seen: set[str] = set()
        for n in items:
            bullet = n.bullet.replace("<!--", "").replace("-->", "")
            bullet = " ".join(bullet.split())
            if not bullet or bullet in seen:
                continue
            seen.add(bullet)
            lines.append(f"- {bullet}")
    lines.append(BLOCK_END)
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------- reading a block
@dataclass
class PackageNotes:
    """One package's section of a block."""

    version: str
    # Format 2 only: the release the notes compare from, "(compared from 0.56.0)" (a block
    # written before there was a margin: "(0.60.0 at the cutoff)").
    cutoff_version: str | None
    bullets: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)  # (text, tags)


@dataclass
class ParsedBlock:
    """What :func:`parse_block` reads from a block."""

    version: int  # BLOCK_VERSION, or 1 for a block 0.3 wrote (no meta line)
    # The meta line's fields; for a 0.3 block, the model, cutoff and versions_from its header
    # names ({} when it does not read as 0.3's).
    meta: dict[str, Any]
    packages: dict[str, PackageNotes]
    # Whether the text after the meta line differs from what its hash says: edited by hand
    # (or the meta line is unreadable). None for a 0.3 block, which has no hash.
    edited: bool | None


_LEGACY_HEADER = re.compile(
    r"Generated by \[since-cutoff\]\([^)]*\) for `(?P<model>[^`]*)` \(training cutoff "
    r"(?P<cutoff>\d{4}-\d{2}-\d{2})\), checked against `(?P<source>[^`]*)`\."
)
# ``**anthropic 1.8.0** (compared from 0.56.0)``, or, from a block written before there was a
# margin, ``**anthropic 1.8.0** (0.60.0 at the cutoff)``.
_PACKAGE_LINE = re.compile(
    r"^\*\*(?P<pkg>\S+) (?P<version>\S+)\*\*"
    r"(?: \((?:compared from (?P<cutoff>\S+)|(?P<at_cutoff>\S+) at the cutoff)\))?$"
)


def parse_block(text: str, name: str = "AGENTS.md") -> ParsedBlock | None:
    """The since-cutoff block in the text of a file (``name`` is for error messages), or None
    when it has none. Reads format 2 and 0.3's blocks. Raises NotesFileError for markers it
    cannot safely read (unbalanced or repeated)."""
    span = _block_span(text, Path(name))
    if span is None:
        return None
    lines = text[span[0] : span[1]].replace("\r\n", "\n").rstrip("\n").split("\n")
    lines = [line.strip() if i in (0, len(lines) - 1) else line for i, line in enumerate(lines)]
    inner = lines[1:-1]
    meta: dict[str, Any] = {}
    edited: bool | None = None
    version = 1
    if inner and inner[0].startswith(META_START) and inner[0].endswith(META_END):
        version = BLOCK_VERSION
        raw = inner[0][len(META_START) : -len(META_END)]
        try:
            loaded = json.loads(raw)
            meta = loaded if isinstance(loaded, dict) else {}
        except ValueError:
            meta = {}
        inner = inner[1:]
        body = "\n".join(inner) + "\n"
        edited = not meta or meta.get("body") != body_hash(body)
        v = meta.get("v")
        version = v if isinstance(v, int) and v >= BLOCK_VERSION else BLOCK_VERSION
    else:
        head = _LEGACY_HEADER.search("\n".join(inner))
        if head is not None:
            meta = {
                "v": 1,
                "model": head.group("model"),
                "cutoff": head.group("cutoff"),
                "versions_from": head.group("source"),
            }
    packages: dict[str, PackageNotes] = {}
    current: PackageNotes | None = None
    for line in inner:
        m = _PACKAGE_LINE.match(line.strip())
        if m is not None:
            current = PackageNotes(m.group("version"), m.group("cutoff") or m.group("at_cutoff"))
            packages[m.group("pkg")] = current
        elif current is not None and line.startswith("- "):
            current.bullets.append(split_tags(line[2:].strip()))
    return ParsedBlock(version, meta, packages, edited)


# ------------------------------------------------------------ the target file
# The instructions files a coding assistant reads, where the block goes unless --target says.
INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md")


def default_target(root: Path, explicit: str | None = None) -> Path:
    """The file for a first block: ``explicit`` (``--target``, relative to the project), else
    AGENTS.md, or CLAUDE.md when only that one exists.

    Claude Code reads only CLAUDE.md when both exist; writing to both, or to AGENTS.md only
    when CLAUDE.md imports it with ``@AGENTS.md``, is issue #13 (not changed here; see
    :func:`block_targets`).
    """
    if explicit:
        p = Path(explicit)
        return p if p.is_absolute() else root / p
    agents, claude = root / "AGENTS.md", root / "CLAUDE.md"
    if agents.exists() or not claude.exists():
        return agents
    return claude


def block_targets(root: Path, explicit: str | None = None) -> list[Path]:
    """The files the notes block goes into (``run --apply``, ``sync``, ``status``):

    1. ``explicit`` (``--target``, relative to the project), alone;
    2. otherwise each of AGENTS.md and CLAUDE.md that already has a block (or since-cutoff
       markers that cannot be read, which reading it then reports), so that a block stays
       where it was written (``run --apply --target CLAUDE.md``) and none is left behind;
    3. otherwise :func:`default_target`: AGENTS.md, or CLAUDE.md when only that one exists.

    Issue #13 (a good first issue) is the case of both files and no block yet. Claude Code
    reads only CLAUDE.md when both exist, and AGENTS.md too when CLAUDE.md imports it with
    ``@AGENTS.md`` (outside code spans and fenced blocks). The plan for it: with that import,
    AGENTS.md only (removing a block left in CLAUDE.md); without it, both files, with the tip
    "add `@AGENTS.md` to CLAUDE.md to keep one copy". Until then that case keeps 0.3's
    choice, AGENTS.md.
    """
    if explicit:
        return [default_target(root, explicit)]
    found = [root / name for name in INSTRUCTION_FILES if has_markers(root / name)]
    return found or [default_target(root)]


# What says to add: ``@AGENTS.md`` in CLAUDE.md is how Claude Code reads AGENTS.md too.
AGENTS_IMPORT_TIP = (
    "CLAUDE.md does not import AGENTS.md, so Claude Code does not read the notes in AGENTS.md: "
    "add a line `@AGENTS.md` to CLAUDE.md"
)


def agents_import_tip(root: Path, targets: Sequence[Path]) -> str | None:
    """:data:`AGENTS_IMPORT_TIP` when the notes go to AGENTS.md (``targets``) and not to
    CLAUDE.md, although a CLAUDE.md exists that does not mention ``@AGENTS.md``: Claude Code
    then reads only CLAUDE.md. None otherwise.

    A plain text search, so ``@AGENTS.md`` inside a code span counts too: choosing the files
    for this case (both files, or AGENTS.md alone with the import) is issue #13, which will
    also read the import as Claude Code does, outside code; this tip is only until then.
    """
    agents, claude = root / "AGENTS.md", root / "CLAUDE.md"
    if agents not in targets or claude in targets or not claude.is_file():
        return None
    try:
        text = _read(claude)
    except (OSError, UnicodeDecodeError):
        return None
    return None if re.search(r"@(?:\./)?AGENTS\.md", text) else AGENTS_IMPORT_TIP


_MARKER_LINE = re.compile(
    rf"(?m)^\ufeff?[ \t]*(?:{re.escape(BLOCK_START)}|{re.escape(BLOCK_END)})[ \t]*\r?$"
)
# A start marker, after the byte order mark some editors put first in a file (it is not part
# of the block: the span starts after it, so that writing the block keeps it).
_START_LINE = re.compile(rf"(?m)^\ufeff?(?P<line>[ \t]*{re.escape(BLOCK_START)}[ \t]*\r?$)")
_END_LINE = re.compile(rf"(?m)^[ \t]*{re.escape(BLOCK_END)}[ \t]*\r?$")
# The meta line's key that says since-cutoff put a line break after the last line of a file
# that had none, and a blank line, before appending the block: removing the block takes both.
ADDED_EOL = "added_eol"


def has_markers(path: Path) -> bool:
    """Whether ``path`` has a line with a since-cutoff marker (a block, or a broken one)."""
    try:
        return _MARKER_LINE.search(_read(path)) is not None
    except (OSError, UnicodeDecodeError):
        return False


def read_text(path: Path) -> str | None:
    """The text of an instructions file, line breaks as they are; None when there is none."""
    if not path.exists():
        return None
    try:
        return _read(path)
    except UnicodeDecodeError as exc:
        raise NotesFileError(f"{path.name} is not UTF-8 text: {exc}") from exc
    except OSError as exc:
        raise NotesFileError(f"cannot read {path}: {exc}") from exc


def block_text(text: str, name: str = "AGENTS.md") -> str | None:
    """The since-cutoff block in ``text`` as :func:`render_block` would have written it: LF line
    breaks, one at the end (also when the file ends right after the end marker), and without
    what :func:`with_block` records about the file itself (ADDED_EOL). None when there is none
    (NotesFileError for markers that cannot be read, as :func:`parse_block`)."""
    span = _block_span(text, Path(name))
    if span is None:
        return None
    block = _meta_without(text[span[0] : span[1]].replace("\r\n", "\n"), ADDED_EOL)
    return block if block.endswith("\n") else block + "\n"


# ------------------------------------------------------------- writing it
class NotesFileError(SinceCutoffError):
    """AGENTS.md/CLAUDE.md has markers since-cutoff cannot safely edit, or cannot be read or
    written."""


def _read(path: Path) -> str:
    with path.open(encoding="utf-8", newline="") as fh:
        return fh.read()


def _write(path: Path, text: str) -> None:
    """Write the file, and the folders it is in (``--target docs/RULES.md``); NotesFileError
    when that fails (a read-only file, a path that is a file's)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as fh:
            fh.write(text)
    except OSError as exc:
        raise NotesFileError(f"cannot write {path}: {exc}") from exc


def _block_span(text: str, path: Path) -> tuple[int, int] | None:
    """Character span of the single well-formed block (markers on their own lines), if any:
    from its start marker's line (after a byte order mark) to the line break after its end
    marker, or to the end of the file when none follows."""
    starts = [m.start("line") for m in _START_LINE.finditer(text)]
    ends = [m.end() for m in _END_LINE.finditer(text)]
    if not starts and not ends:
        return None
    if len(starts) != 1 or len(ends) != 1 or ends[0] < starts[0]:
        raise NotesFileError(
            f"{path.name} has unbalanced or repeated since-cutoff markers; fix or remove them by "
            "hand, then run again"
        )
    end = ends[0]
    if text[end : end + 2] == "\r\n":
        end += 2
    elif text[end : end + 1] == "\n":
        end += 1
    return starts[0], end


def _meta_of(block: str) -> dict[str, Any] | None:
    """The meta line of a block's text (LF line breaks), or None when it has none."""
    lines = block.split("\n", 2)
    if len(lines) < 2:
        return None
    line = lines[1].strip()
    if not (line.startswith(META_START) and line.endswith(META_END)):
        return None
    try:
        meta = json.loads(line[len(META_START) : -len(META_END)])
    except ValueError:
        return None
    return meta if isinstance(meta, dict) else None


def _meta_with(block: str, key: str, value: Any) -> str:
    """The block (LF line breaks) with ``key`` set in its meta line (the block unchanged when it
    has none)."""
    meta = _meta_of(block)
    if meta is None:
        return block
    first, _, rest = block.split("\n", 2)
    return f"{first}\n{_meta_line({**meta, key: value})}\n{rest}"


def _meta_without(block: str, key: str) -> str:
    meta = _meta_of(block)
    if meta is None or key not in meta:
        return block
    first, _, rest = block.split("\n", 2)
    return f"{first}\n{_meta_line({k: v for k, v in meta.items() if k != key})}\n{rest}"


def with_block(text: str | None, block: str, name: str = "AGENTS.md") -> tuple[str, str]:
    """The text of a file (None: there is no such file) with the since-cutoff block inserted
    or replaced, and what that does: "created", "appended to" or "updated".

    Only a block whose markers sit on their own lines is replaced; anything else in the file is
    left byte-for-byte unchanged (including its line endings, and a file that ends right after
    the end marker without a line break stays so). An appended block has a blank line before
    it; after a last line without a line break, since-cutoff adds one and says so in the meta
    line (ADDED_EOL), so that :func:`without_block` gives back the file as it was.
    """
    if text is None:
        return block, "created"
    nl = "\r\n" if "\r\n" in text else "\n"
    lf = block.replace("\r\n", "\n")
    span = _block_span(text, Path(name))
    if span is not None:
        old = text[span[0] : span[1]].replace("\r\n", "\n")
        if (_meta_of(old) or {}).get(ADDED_EOL) is True:
            lf = _meta_with(lf, ADDED_EOL, True)
        if not old.endswith("\n"):
            lf = lf.rstrip("\n")
        return text[: span[0]] + lf.replace("\n", nl) + text[span[1] :], "updated"
    if not text:
        return lf.replace("\n", nl), "appended to"
    if text.endswith("\n") or _meta_of(lf) is None:
        # One line break before the block, which makes a blank line after a last line that
        # has its own; remove_block takes exactly that one away again.
        return text + nl + lf.replace("\n", nl), "appended to"
    return text + nl + nl + _meta_with(lf, ADDED_EOL, True).replace("\n", nl), "appended to"


def without_block(text: str, name: str = "AGENTS.md") -> str | None:
    """The text of a file without its since-cutoff block, or None when it has none.

    Removing an appended block gives back the file as it was before, byte for byte. A block
    with text after it (moved there by hand) leaves one blank line between the text before
    and after it; every other line stays as it is.
    """
    span = _block_span(text, Path(name))
    if span is None:
        return None
    nl = "\r\n" if "\r\n" in text else "\n"
    before, after = text[: span[0]], text[span[1] :]
    if after.strip(" \t\r\n"):
        before = re.sub(r"(?m)(?:^[ \t]*\r?\n)+\Z", "", before)  # the blank lines around it
        after = re.sub(r"\A(?:[ \t]*\r?\n)+", "", after)
        return before + (nl if before else "") + after
    # The block ends the file, as apply_block leaves it, after the line break(s) it added.
    meta = _meta_of(text[span[0] : span[1]].replace("\r\n", "\n")) or {}
    if meta.get(ADDED_EOL) is True and before.endswith(nl + nl):
        return before[: -2 * len(nl)]
    return before[: -len(nl)] if before.endswith(nl) else before


def apply_block(path: Path, block: str) -> str:
    """Insert or replace the since-cutoff block in ``path`` (:func:`with_block`). Returns what
    was done; the file is only written when its text changes. NotesFileError when it cannot be
    read or written."""
    text = read_text(path)
    new, action = with_block(text, block, path.name)
    if new != text:
        _write(path, new)
    return action


def remove_block(path: Path) -> bool:
    """Remove the since-cutoff block (:func:`without_block`). Returns False (and leaves the file
    alone) if there is none."""
    text = read_text(path)
    if text is None:
        return False
    new = without_block(text, path.name)
    if new is None:
        return False
    _write(path, new)
    return True
