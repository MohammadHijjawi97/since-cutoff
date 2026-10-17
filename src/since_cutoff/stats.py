"""Small statistics helpers. Every rate since-cutoff prints comes with its sample size.

Pure Python on purpose: the samples are small (tens of changes), and a numeric dependency
would weigh more than the arithmetic it saves.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence

# The cluster bootstrap is seeded, so the same answers always give the same interval.
BOOTSTRAP_RESAMPLES = 4000
BOOTSTRAP_SEED = 0


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a binomial proportion (well behaved for small n)."""
    if n <= 0:
        return (0.0, 1.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    # The bounds contain the observed rate: rounding put 0 of 11 at 2.8e-17, 6 of 6 below 1.
    return (max(0.0, min(p, centre - half)), min(1.0, max(p, centre + half)))


def sign_test(positive: int, negative: int) -> float:
    """Exact two-sided sign test: the p-value of ``positive`` vs ``negative`` under p = 1/2.

    Ties are left out before calling. The binomial distribution with p = 1/2 is symmetric, so
    the two-sided p-value is twice the smaller tail, capped at 1: 4 vs 0 gives 0.125, 6 vs 0
    gives 0.03125, and 0 vs 0 (nothing to compare) gives 1.
    """
    if positive < 0 or negative < 0:
        raise ValueError("counts must be 0 or more")
    n = positive + negative
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, k) for k in range(min(positive, negative) + 1))
    return min(1.0, 2 * tail / (1 << n))


def cluster_bootstrap_interval(
    clusters: Sequence[tuple[int, int]],
    *,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
    level: float = 0.95,
) -> tuple[float, float] | None:
    """Percentile bootstrap interval for a pooled paired difference, resampling whole clusters.

    Each cluster is ``(pairs, net)``: how many paired observations it holds and the sum of
    their differences (each +1, 0 or -1). One resample draws as many clusters as there are,
    with replacement, and computes ``sum(net) / sum(pairs)``, the difference of the two pooled
    rates. Pairs of one cluster stay together, because they are not independent (held-out tasks
    of one API change succeed or fail together). None with fewer than two clusters, where
    resampling says nothing.
    """
    kept = [(n, net) for n, net in clusters if n > 0]
    if len(kept) < 2:
        return None
    rng = random.Random(seed)
    k = len(kept)
    estimates: list[float] = []
    for _ in range(max(1, resamples)):
        drawn = rng.choices(kept, k=k)
        estimates.append(sum(net for _, net in drawn) / sum(n for n, _ in drawn))
    estimates.sort()
    tail = (1 - level) / 2
    return (_quantile(estimates, tail), _quantile(estimates, 1 - tail))


def _quantile(ordered: Sequence[float], q: float) -> float:
    """The ``q`` quantile of sorted values, interpolating linearly (numpy's default method)."""
    pos = q * (len(ordered) - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def percent(successes: int, n: int) -> int:
    """A rate as a whole percentage, rounded as :func:`pct` prints it (half to even)."""
    return round(100 * successes / n)


def pct(successes: int, n: int) -> str:
    return "n/a" if n <= 0 else f"{percent(successes, n)}%"


def points(fraction: float) -> str:
    """A difference of two rates in percentage points, always signed: ``+60``, ``-5``, ``+0``."""
    return f"{round(100 * fraction):+d}"


def points_between(before: int, after: int, n: int) -> str:
    """``after/n - before/n`` in percentage points, as the difference of the two rates that
    :func:`pct` prints: 1/8 -> 6/8 is ``12%`` -> ``75%``, so ``+63`` (the exact +62.5 would
    round to ``+62``, which the printed rates do not add up to)."""
    return f"{percent(after, n) - percent(before, n):+d}"


def format_p(p: float) -> str:
    """``p=0.031``, ``p=0.125``, ``p=1`` (three decimals, trailing zeros dropped) or ``p<0.001``."""
    if p < 0.001:
        return "p<0.001"
    return "p=" + f"{p:.3f}".rstrip("0").rstrip(".")


def estimate_tokens(text: str) -> int:
    """Rough token count (about 4 characters per token for English and code)."""
    return max(1, round(len(text) / 4))
