"""Estimates from samples (spec 8.6): Wilson intervals, scaled counts, labelled values."""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal


@dataclass(frozen=True)
class EstimatedValue:
    """A number measured on a sample. ``lower_bound`` marks distinct counts and maxima from a
    block/Bernoulli sample: the true value is at least ``value`` (``high`` is then ``None``).
    ``approximate`` marks block (SYSTEM) samples: clustered rows, so the interval is widened by a
    design effect and is only approximate (spec 8.6)."""

    value: float | int | None
    low: float | None
    high: float | None
    method: str
    lower_bound: bool
    approximate: bool = False


def wilson(
    successes: int, n: int, z: float = 1.96, design_effect: float = 1.0
) -> tuple[float, float] | None:
    """Wilson score interval for ``successes / n``; ``None`` when ``n == 0`` (unknown).

    Clustered (block) samples pass ``design_effect > 1``: the effective sample size is
    ``n / design_effect`` at the same proportion, which widens the interval.
    """
    if n < 0 or successes < 0 or successes > n:
        raise ValueError(f"need 0 <= successes <= n, got {successes}/{n}")
    if design_effect < 1.0:
        raise ValueError(f"design_effect must be >= 1, got {design_effect}")
    if n == 0:
        return None
    p = successes / n
    n_eff = n / design_effect
    z2 = z * z
    denom = 1.0 + z2 / n_eff
    centre = (p + z2 / (2.0 * n_eff)) / denom
    half = z * math.sqrt(p * (1.0 - p) / n_eff + z2 / (4.0 * n_eff * n_eff)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def scale_count(count: int, fraction: Decimal) -> int:
    """A count seen in a ``fraction`` sample, scaled to the whole table (half-even rounding)."""
    if not Decimal(0) < fraction <= 1:
        raise ValueError(f"fraction must be in (0, 1], got {fraction}")
    return int((Decimal(count) / fraction).to_integral_value(rounding=ROUND_HALF_EVEN))
