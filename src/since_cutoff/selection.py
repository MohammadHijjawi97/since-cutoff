"""Choose which API changes to probe when there are more than the budget allows.

Ranking favours changes that are most likely to bite this project: symbols the project's own
code already touches, hard breaks over soft ones, top-level exports over deep internals, and
changes that recur across many call sites. Near-duplicates are probed once, and the budget is
shared round-robin across packages so one huge migration cannot crowd out everything else.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import replace

from since_cutoff.apidiff import DEPRECATED, KIND_PRIORITY, APIChange

_NOISE_NAMES = {"__version__", "__all__", "version", "VERSION"}
_METADATA_NAMES = {"__version__", "__all__", "__version_tuple__", "version_tuple"}
# Modules that application code rarely imports directly.
_SIDE_SEGMENTS = {"cli", "commands", "test", "testing", "experimental"}
# Integration and implementation layers: type-checker plugins (pydantic.mypy), compatibility
# shims, protocol adapters (polars.interchange) and per-backend code (sqlalchemy.dialects).
_INTERNAL_SEGMENTS = {
    "compat",
    "connectors",
    "dialects",
    "interchange",
    "internal",
    "internals",
    "mypy",
    "plugin",
    "plugins",
}


def score(change: APIChange, identifiers: set[str]) -> float:
    s = 10.0 - 1.5 * KIND_PRIORITY.get(change.kind, 6)
    s += 3.0 * usage(change, identifiers)
    if change.owner and change.owner in identifiers:
        s += 2.0
    depth = change.path.count(".")
    s += max(0.0, 3.0 - 0.75 * depth)
    s += min(2.0, math.log2(change.occurrences + 1) - 1)
    if change.old_doc:
        s += 1.0
    if change.hint:
        s += 0.5
    if change.name in _NOISE_NAMES or (change.name.isupper() and change.owner):
        s -= 4.0  # enum members and constants rarely matter
    segments = change.path.split(".")[1:-1]  # modules (and owner), not the package or name
    if any(seg in _INTERNAL_SEGMENTS for seg in segments):
        s -= 3.0
    elif any(seg in _SIDE_SEGMENTS for seg in segments):
        s -= 2.0
    if (
        change.path.count(".") == 1
        and change.kind in ("removed", "moved")
        and change.old_signature
        and change.old_signature.startswith("module ")
    ):
        s -= 1.0
    return s


def rank(changes: Iterable[APIChange], identifiers: set[str]) -> list[APIChange]:
    return sorted(changes, key=lambda c: (-score(c, identifiers), c.path, c.parameter or ""))


def collapse(changes: Iterable[APIChange]) -> list[APIChange]:
    """One entry per change, most useful first.

    The same change is often reachable under several public paths (``pkg.f`` and
    ``pkg.module.f``); it is listed once, under its shortest path, with the others counted as
    similar. Module metadata such as ``__version__`` is dropped.
    """
    ordered = sorted(
        changes, key=lambda c: (-score(c, set()), c.path.count("."), c.path, c.parameter or "")
    )
    firsts: dict[str, APIChange] = {}
    for c in ordered:
        if c.name in _METADATA_NAMES:
            continue
        rep = firsts.get(c.concept_key)
        if rep is None:
            firsts[c.concept_key] = replace(c, also=list(c.also))
        else:
            rep.occurrences += c.occurrences
            rep.also = [*rep.also, c.path, *c.also][:5]
    return list(firsts.values())


def used_names(change: APIChange, identifiers: set[str]) -> tuple[str, ...]:
    """The names of a change that the project's code uses.

    ``()``, ``(callable,)`` or ``(callable, parameter)``. The changed callable (the class, for
    a constructor) must be named in the code. A method or attribute also needs its class,
    directly or as an attribute (``client.messages`` for ``Messages``), so a common name such as
    ``create`` does not match every class that has one. A changed parameter counts only together
    with its callable: ``resume_download=`` passed to ``hf_hub_download`` says nothing about
    ``snapshot_download(resume_download=...)``.
    """
    owner, name = change.owner, change.name
    if name == "__init__":
        callable_name = owner
        named = bool(owner) and owner in identifiers
    else:
        callable_name = name
        named = not name.startswith("__") and name in identifiers
        if named and owner:  # a method or attribute: its class must be named as well
            named = owner in identifiers or owner.lower() in identifiers
    if not named or not callable_name:
        return ()
    parameter = change.parameter
    if parameter and not parameter.startswith("__") and parameter in identifiers:
        return (callable_name, parameter)
    return (callable_name,)


def usage(change: APIChange, identifiers: set[str]) -> int:
    """How directly the project's code touches a change.

    2: it names exactly what changed (the changed name, or a callable and its changed
    parameter); 1: it names the callable but not the changed parameter; 0: neither.
    """
    names = used_names(change, identifiers)
    if not names:
        return 0
    return 2 if len(names) == 2 or not change.parameter else 1


def used_name(change: APIChange, identifiers: set[str]) -> str | None:
    """The most specific name of the change that the project's code uses, if any."""
    names = used_names(change, identifiers)
    return names[-1] if names else None


def uses_text(change: APIChange, identifiers: set[str]) -> str | None:
    """The names for a "your code uses ..." mark, as Markdown code (None if it uses none)."""
    names = used_names(change, identifiers)
    return " and ".join(f"`{n}`" for n in names) if names else None


def project_rank(change: APIChange, identifiers: set[str]) -> tuple[int, bool, float, str]:
    """Sort key for one package's changes as seen from a project.

    What the code uses comes first (a callable and its changed parameter before the callable
    alone), then hard breaks before deprecations, then the score.
    """
    return (
        -usage(change, identifiers),
        change.kind == DEPRECATED,
        -score(change, identifiers),
        change.path,
    )


def select(
    changes_by_package: dict[str, list[APIChange]],
    identifiers: set[str],
    budget: int,
) -> list[APIChange]:
    """Pick up to ``budget`` changes, one per concept, spread round-robin across packages."""
    queues: dict[str, list[APIChange]] = {}
    for package, changes in changes_by_package.items():
        seen: set[str] = set()
        queue: list[APIChange] = []
        for c in rank(changes, identifiers):
            if c.concept_key in seen:
                continue
            seen.add(c.concept_key)
            queue.append(c)
        if queue:
            queues[package] = queue
    # Packages whose best change scores highest go first in each round.
    order = sorted(queues, key=lambda p: -score(queues[p][0], identifiers))
    picked: list[APIChange] = []
    while len(picked) < budget and any(queues.values()):
        for package in order:
            if queues[package] and len(picked) < budget:
                picked.append(queues[package].pop(0))
    return picked
