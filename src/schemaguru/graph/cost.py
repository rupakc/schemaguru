"""Cost of one hop (spec 9.2): additive, lower is better.

``cost = W_SUPPORT * -ln(support(direction)) + W_FANOUT * ln(1 + distinct_left)
+ W_HUB * ln(1 + in_degree(arrival_table))``.

Every weight is read from the :class:`GraphConfig` passed at call time, so a sweep over weights
is falsifiable. ``support == 0`` costs ``math.inf`` (the hop is never taken). An unknown support
(unmeasured edge, or a reverse hop whose coverage was not measured) pays ``unmeasured_penalty``
instead of the support term; an unknown ``distinct_left`` pays ``W_FANOUT * unmeasured_penalty``
instead of the fan-out term, so unknown is never cheaper than known.

The fan-out term is a weak proxy: in the predecessor it correlated only -0.30 with true
rows-per-key. It stays because nothing measured replaces it yet.
"""

from __future__ import annotations

import math

from schemaguru.config import GraphConfig
from schemaguru.graph.edges import Edge
from schemaguru.model.joins import support


def hop_support(edge: Edge, *, reverse: bool) -> float | None:
    """``match_rate`` child -> parent, ``coverage`` parent -> child; ``None`` if unknown."""
    if not edge.measured:
        return None
    return support(edge.value, reverse=reverse)


def hop_cost(edge: Edge, *, reverse: bool, in_degree: int, cfg: GraphConfig) -> float:
    s = hop_support(edge, reverse=reverse)
    if s is None:
        support_term = cfg.unmeasured_penalty
    elif s <= 0.0:
        return math.inf
    else:
        support_term = cfg.w_support * -math.log(min(s, 1.0))
    dl = edge.value.get("distinct_left") if edge.measured else None
    if dl is None:
        fan_term = cfg.w_fanout * cfg.unmeasured_penalty
    else:
        fan_term = cfg.w_fanout * math.log1p(float(dl))
    hub_term = cfg.w_hub * math.log1p(max(in_degree, 0))
    return support_term + fan_term + hub_term
