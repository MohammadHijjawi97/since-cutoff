"""Baseline notes for ``run --compare``: what a run's notes are measured against.

Each baseline turns the failing API changes into a block with no model call, in the format 0.3
wrote (:func:`notes.render_legacy_block`, same header), so that the numbers measured with them
stay comparable with 0.3's:

- **template**: one plain sentence per change from the API diff (:func:`notes.template_bullet`),
  the text 0.3 fell back to when a model's note did not type-check.
- **signatures**: what the new version's own API says about the API to use, and nothing about
  what changed: the signature and first docstring paragraph of the changed callable (a moved
  object at its new path, a changed ``__init__`` as its class). For a removal or a deprecation
  whose library names a replacement that exists in the new version (the old version's
  deprecation note, the new version's ``@deprecated`` message), the replacement's instead. A
  removed object also gets a bare "is not in" line, since the new version has nothing else to
  say about it.

The engine answers every held-out task once without notes and once per block, so every
baseline is paired against the same answers without notes as the run's notes
(:meth:`engine.RunResult.pairing`).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, NamedTuple

from since_cutoff.apidiff import (
    DEPRECATED,
    KIND_CHANGED,
    MOVED,
    REMOVED,
    APIChange,
    candidate_paths,
    doc_summary,
    find_object,
    is_callable_api,
    signature_of,
)
from since_cutoff.apidiff import (  # where it lived before 0.4.0
    replacement_candidates as replacement_candidates,
)
from since_cutoff.notes import Note, safe_text, template_bullet

# The notes blocks a run can test (``run --compare``). The run's own notes are always one;
# their id is still ``verified`` (results.json), as it has been since 0.2.
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
    for path in candidate_paths(change, text, root.path):
        obj = find_object(root, path)
        if is_callable_api(obj, root.path):
            return Reference(path, signature_of(obj), doc_summary(obj))
    return None


def _code(text: str) -> str:
    """Text for a Markdown code span: one line, no backtick that would end the span."""
    return " ".join(text.split()).replace("`", "'")
