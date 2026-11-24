"""Hub calibration helper (spec 9.4).

``ATTRIBUTION_FANIN`` was calibrated on the predecessor's own data. This reports, for every sink
of the edge set, the measured fan-in of each edge into it and proposes a threshold at the centre
of the widest gap between consecutive fan-ins on a log scale (the geometric mean of the gap's
ends). The user accepts a proposal into config; nothing here changes configuration.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import pairwise

from schemaguru.graph.edges import Edge
from schemaguru.graph.hubs import sinks
from schemaguru.model.refs import ObjectRef


@dataclass(frozen=True)
class HubCalibration:
    hub: ObjectRef
    fan_ins: list[float]
    proposed_threshold: float | None
    gap: tuple[float, float] | None


def _widest_gap(values: list[float]) -> tuple[float, float] | None:
    pos = [v for v in values if v > 0]
    best: tuple[float, float] | None = None
    best_ratio = 0.0
    for a, b in pairwise(pos):
        ratio = math.log(b) - math.log(a)
        if ratio > best_ratio:  # strict: the first of equal gaps wins (deterministic)
            best, best_ratio = (a, b), ratio
    return best


def calibrate_hubs(edges: Iterable[Edge]) -> list[HubCalibration]:
    es = [e for e in edges if e.measured]
    out: list[HubCalibration] = []
    for sink in sorted(sinks(es), key=lambda r: r.canonical()):
        fans = sorted(
            float(e.value["fan_in"])
            for e in es
            if e.right_table == sink and e.value.get("fan_in") is not None
        )
        gap = _widest_gap(fans)
        proposed = None if gap is None else math.sqrt(gap[0] * gap[1])
        out.append(HubCalibration(sink, fans, proposed, gap))
    return out
