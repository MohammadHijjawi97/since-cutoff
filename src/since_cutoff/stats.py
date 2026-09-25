"""Small statistics helpers. Every rate since-cutoff prints comes with its sample size."""

from __future__ import annotations

import math


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a binomial proportion (well behaved for small n)."""
    if n <= 0:
        return (0.0, 1.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def pct(successes: int, n: int) -> str:
    return "n/a" if n <= 0 else f"{100 * successes / n:.0f}%"


def estimate_tokens(text: str) -> int:
    """Rough token count (about 4 characters per token for English and code)."""
    return max(1, round(len(text) / 4))
