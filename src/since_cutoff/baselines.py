"""Baseline notes for ``run --compare``: what the verified notes are measured against.

Each baseline turns the failing API changes into a block in the same format as the verified
notes (:func:`notes.render_block`, same header), with no model call:

- **template**: one plain sentence per change from the API diff (:func:`notes.template_bullet`),
  the text a verified note falls back to when it cannot be verified.
- **signatures**: what the new version's own API says about the API to use, and nothing about
  what changed: the signature and first docstring paragraph of the changed callable (a moved
  object at its new path, a changed ``__init__`` as its class). For a removal or a deprecation
  whose library names a replacement that exists in the new version (the old version's
  deprecation note, the new version's ``@deprecated`` message), the replacement's instead. A
  removed object also gets a bare "is not in" line, since the new version has nothing else to
  say about it.

The engine answers every held-out task once without notes and once per block, so every
baseline is paired against the same answers without notes as the verified notes
(:meth:`engine.RunResult.pairing`).
"""

from __future__ import annotations

import keyword
import re
from collections.abc import Callable, Iterable
from typing import Any, NamedTuple

from since_cutoff.apidiff import (
    DEPRECATED,
    KIND_CHANGED,
    MOVED,
    REMOVED,
    APIChange,
    doc_summary,
    find_object,
    signature_of,
)
from since_cutoff.notes import Note, safe_text, template_bullet

# The notes blocks a run can verify (``run --compare``). The verified notes are always one.
ARM_VERIFIED = "verified"
ARM_TEMPLATE = "template"
ARM_SIGNATURES = "signatures"
BASELINE_ARMS = (ARM_TEMPLATE, ARM_SIGNATURES)

# The new version's API (a griffe module from apidiff.load_api) that holds a change, or None.
ApiLookup = Callable[[APIChange], Any]


class Reference(NamedTuple):
    """An API of the new version as the signatures baseline shows it."""

    path: str  # public path, as the bullet names it
    signature: str | None  # apidiff.signature_of
    doc: str | None  # first docstring paragraph (apidiff.doc_summary)


_CODE_SPAN = re.compile(r"`([^`]+)`")
_ROLE = re.compile(r":(?:\w+:)+(?=`)")  # Sphinx roles: :func:`x`, :py:meth:`x`
_NAME = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")
# Words of deprecation notes that are never the replacement's name.
_PROSE = {
    "use",
    "using",
    "prefer",
    "instead",
    "replace",
    "replacement",
    "alternative",
    "deprecated",
    "deprecation",
    "removed",
    "will",
    "the",
    "and",
    "for",
    "with",
    "from",
    "this",
    "that",
    "since",
    "version",
    "release",
    "future",
    "please",
    "favor",
    "favour",
    "replaced",
    "now",
    "new",
    "old",
    "call",
    "see",
    "not",
    "has",
    "been",
    "was",
    "are",
    "its",
    "method",
    "function",
    "class",
    "argument",
    "parameter",
    "attribute",
    "module",
    "warning",
}


def template_notes(changes: Iterable[APIChange]) -> list[Note]:
    """The ``template`` baseline: :func:`notes.template_bullet` for each change."""
    return [Note(c, template_bullet(c), None, False, ARM_TEMPLATE) for c in changes]


def signature_notes(changes: Iterable[APIChange], api: ApiLookup) -> list[Note]:
    """The ``signatures`` baseline: :func:`signature_bullets` for each change.

    An API is shown once, even when two changes lead to it under different paths (``pkg.fetch``
    as the replacement of a removed function, then ``pkg.helpers.fetch`` as a changed one).
    Paths are compared as the objects they name in the new API when that API was loaded for a
    replacement anyway, and as written otherwise; the new API is never loaded just for this.
    """
    roots: dict[str, list[Any]] = {}

    def recording(change: APIChange) -> Any:
        root = api(change)
        if root is not None and all(r is not root for r in roots.get(change.package, [])):
            roots.setdefault(change.package, []).append(root)
        return root

    items = [(c, _signature_items(c, recording)) for c in changes]
    notes: list[Note] = []
    shown: set[tuple[str, str]] = set()
    for change, its in items:
        for item in its:
            if isinstance(item, Reference):
                identity = (change.package, _canonical(item.path, roots.get(change.package, [])))
                if identity in shown:
                    continue
                shown.add(identity)
            notes.append(Note(change, _bullet(item), None, False, ARM_SIGNATURES))
    return notes


def _canonical(path: str, roots: list[Any]) -> str:
    """Where ``path`` is defined, in the first loaded API that has it; else ``path`` itself."""
    for root in roots:
        obj = find_object(root, path)
        if obj is not None:
            return str(getattr(obj, "path", path))
    return path


def signature_bullets(change: APIChange, api: ApiLookup) -> list[str]:
    """What the new version says about the API to use instead of ``change`` (module docstring).

    ``api`` is only called for a removal or deprecation that names a replacement, and for a
    kind change whose new docstring the diff did not keep.
    """
    return [_bullet(item) for item in _signature_items(change, api)]


def _signature_items(change: APIChange, api: ApiLookup) -> list[Reference | str]:
    """The APIs :func:`signature_bullets` shows for ``change``; a str is a finished bullet."""
    if change.kind == REMOVED:
        gone = f"`{_code(change.path)}` is not in {change.package} {change.to_version}."
        found = replacement(change, api)
        return [gone] if found is None else [gone, found]
    if change.kind == DEPRECATED:
        found = replacement(change, api)
        if found is not None:
            return [found]
    path = change.moved_to if change.kind == MOVED and change.moved_to else change.path
    doc = change.new_doc
    if doc is None and change.kind == KIND_CHANGED:
        root = api(change)
        doc = doc_summary(find_object(root, path)) if root is not None else None
    return [Reference(path, change.new_signature, doc)]


def _bullet(item: Reference | str) -> str:
    return item if isinstance(item, str) else reference(*item)


def reference(path: str, signature: str | None, doc: str | None) -> str:
    """One bullet: the signature under its full ``path``, then the first docstring paragraph.

    Both come from the package, so they are untrusted: a backtick cannot end the code span and
    the docstring is cut to one short line (:func:`notes.safe_text`).
    """
    text = f"`{_code(qualified(signature, path) if signature else path)}`"
    if not doc:
        return text + "."
    doc = safe_text(doc)
    return f"{text}: {doc}" + ("" if doc.endswith((".", "!", "?")) else ".")


def qualified(signature: str, path: str) -> str:
    """``signature`` as :func:`apidiff.signature_of` writes it, named by its full ``path``.

    ``send(self, message: str) -> str`` at ``pkg.Client.send`` becomes
    ``pkg.Client.send(self, message: str) -> str``, and ``__init__(self, key: str)`` at
    ``pkg.Client.__init__`` becomes ``class pkg.Client(self, key: str)``, the way a class's own
    signature reads. A signature whose name is not the path's last part (an alias) is kept.
    """
    owner, _, name = path.rpartition(".")
    if name == "__init__" and owner and signature.startswith("__init__("):
        return f"class {owner}{signature[len('__init__') :]}"
    for prefix in ("class ", ""):
        head = prefix + name
        rest = signature[len(head) :]
        if signature.startswith(head) and (not rest or rest[0] in "(: "):
            return prefix + path + rest
    return signature


def replacement(change: APIChange, api: ApiLookup) -> Reference | None:
    """``(path, signature, doc)`` of the replacement the library names for ``change``, if any.

    The name comes from the old version's deprecation note or the new version's deprecation
    message, and must be a public function or class of the same package in the new version.
    Names that look like code (in backticks, called, dotted, snake_case, CamelCase) are tried
    before plain words; the first that exists wins.
    """
    if change.kind not in (REMOVED, DEPRECATED):
        return None
    text = " ".join(t for t in (change.deprecation, change.hint) if t)
    if not text:
        return None
    root = api(change)
    if root is None:
        return None
    for path in _candidate_paths(change, text, root.path):
        obj = find_object(root, path)
        if _is_callable_api(obj, root.path):
            return Reference(path, signature_of(obj), doc_summary(obj))
    return None


def replacement_candidates(text: str) -> list[str]:
    """Names a deprecation note may give as the replacement, the code-like ones first."""
    text = _ROLE.sub(" ", text)
    code: list[str] = []
    for span in _CODE_SPAN.findall(text):
        code += _NAME.findall(span.lstrip("~"))  # Sphinx's :func:`~pkg.name`
    words: list[str] = []
    prose = _CODE_SPAN.sub(" ", text)
    for m in _NAME.finditer(prose):
        name = m.group()
        called = prose[m.end() : m.end() + 1] == "("
        if called or "." in name or "_" in name or any(c.isupper() for c in name[1:]):
            code.append(name)
        elif len(name) > 2 and name.lower() not in _PROSE and not keyword.iskeyword(name):
            words.append(name)
    return list(dict.fromkeys(code + words))


def _candidate_paths(change: APIChange, text: str, root: str) -> list[str]:
    """Full paths to try for each candidate name: absolute, then in the owner, module, package."""
    scopes = [f"{change.module}.{change.owner}"] if change.owner else []
    scopes += [change.module, root]
    paths: list[str] = []
    for name in replacement_candidates(text):
        if name in (change.name, change.owner) or any(p.startswith("_") for p in name.split(".")):
            continue
        if name == root or name.startswith(root + "."):
            paths.append(name)
        else:
            paths += [f"{scope}.{name}" for scope in scopes]
    return [p for p in dict.fromkeys(paths) if p != change.path]


def _is_callable_api(obj: Any, root: str) -> bool:
    """A function or class defined in the package itself (not a re-exported dependency)."""
    if obj is None or not (getattr(obj, "is_function", False) or getattr(obj, "is_class", False)):
        return False
    top = root.split(".")[0]
    return str(getattr(obj, "path", "")).split(".")[0] == top


def _code(text: str) -> str:
    """Text for a Markdown code span: one line, no backtick that would end the span."""
    return " ".join(text.split()).replace("`", "'")
