"""Choose which API changes to probe when there are more than the budget allows.

Ranking favours changes that are most likely to bite this project: symbols the project's own
code already touches, hard breaks over soft ones, top-level exports over deep internals, and
changes that recur across many call sites. Near-duplicates are probed once, and the budget is
shared round-robin across packages so one huge migration cannot crowd out everything else.
"""

from __future__ import annotations

import math
from collections.abc import Iterable

from since_cutoff.apidiff import KIND_PRIORITY, APIChange

_NOISE_NAMES = {"__version__", "__all__", "version", "VERSION"}


def score(change: APIChange, identifiers: set[str]) -> float:
    s = 10.0 - 1.5 * KIND_PRIORITY.get(change.kind, 6)
    if change.name in identifiers or (change.parameter and change.parameter in identifiers):
        s += 6.0
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
    if any(seg in ("cli", "commands", "testing", "experimental") for seg in change.path.split(".")):
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
