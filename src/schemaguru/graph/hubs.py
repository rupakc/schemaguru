"""Hub guard (spec 9.4).

A *hub* is a sink of the directed edge set (no out-edges) that either has an ``ENTITY_TYPE``
fact or (so the guard works with zero LLM and no overlays) is reached by at least two
attribution edges from distinct tables. An in-edge is an *attribution edge* when its measured
``fan_in`` exceeds ``attribution_fanin``; an unknown ``fan_in`` is not attribution (the guard
cannot see what was never measured). The refused shape (arrive at a hub on an attribution edge
and leave on another) is enforced by the search; a hub as source or target is allowed.
"""

from __future__ import annotations

from collections.abc import Iterable, Set
from dataclasses import dataclass

from schemaguru.config import GraphConfig
from schemaguru.graph.edges import Edge
from schemaguru.model.refs import ObjectRef


def is_attribution(edge: Edge, cfg: GraphConfig) -> bool:
    fan_in = edge.value.get("fan_in") if edge.measured else None
    return fan_in is not None and float(fan_in) > cfg.attribution_fanin


def sinks(edges: Iterable[Edge]) -> set[ObjectRef]:
    """Tables that some edge points into and no edge leaves."""
    es = list(edges)
    lefts = {e.left_table for e in es}
    return {e.right_table for e in es} - lefts


@dataclass(frozen=True)
class Hubs:
    tables: frozenset[ObjectRef]
    # the reason each hub is a hub ("entity type" or "measured shape: ...")
    reasons: tuple[tuple[ObjectRef, str], ...] = ()

    @classmethod
    def from_edges(
        cls, edges: Iterable[Edge], entity_tables: Set[ObjectRef], cfg: GraphConfig
    ) -> Hubs:
        es = list(edges)
        reasons: dict[ObjectRef, str] = {}
        for sink in sorted(sinks(es), key=lambda r: r.canonical()):
            attributing = sorted(
                {
                    e.left_table.canonical()
                    for e in es
                    if e.right_table == sink and is_attribution(e, cfg)
                }
            )
            if sink in entity_tables:
                reasons[sink] = "entity type"
            elif len(attributing) >= 2:
                reasons[sink] = "measured shape: attribution edges from " + ", ".join(attributing)
        return cls(frozenset(reasons), tuple(reasons.items()))

    def is_hub(self, table: ObjectRef) -> bool:
        return table in self.tables

    def reason(self, table: ObjectRef) -> str | None:
        return dict(self.reasons).get(table)
