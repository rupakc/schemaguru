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
    """Whether ``edge`` attributes many child rows to one parent key.

    Judged on the edge alone, whichever way it is traversed: the guard only asks this of edges
    touching a hub, and a hub is a *sink*, so every hub edge points into it (child -> hub) and its
    ``fan_in`` is child rows per hub key in both the arriving and the leaving hop.
    """
    fan_in = edge.value.get("fan_in") if edge.measured else None
    return fan_in is not None and float(fan_in) > cfg.attribution_fanin


def sinks(edges: Iterable[Edge]) -> set[ObjectRef]:
    """Tables that some edge points into and no edge leaves."""
    return set(in_edges_of_sinks(edges))


def in_edges_of_sinks(edges: Iterable[Edge]) -> dict[ObjectRef, list[Edge]]:
    """Each sink with its in-edges, grouped in one pass over the edges."""
    into: dict[ObjectRef, list[Edge]] = {}
    lefts: set[ObjectRef] = set()
    for e in edges:
        lefts.add(e.left_table)
        into.setdefault(e.right_table, []).append(e)
    return {t: es for t, es in into.items() if t not in lefts}


@dataclass(frozen=True)
class Hubs:
    tables: frozenset[ObjectRef]
    # the reason each hub is a hub ("entity type" or "measured shape: ...")
    reasons: tuple[tuple[ObjectRef, str], ...] = ()

    @classmethod
    def from_edges(
        cls, edges: Iterable[Edge], entity_tables: Set[ObjectRef], cfg: GraphConfig
    ) -> Hubs:
        grouped = in_edges_of_sinks(edges)
        reasons: dict[ObjectRef, str] = {}
        for sink in sorted(grouped, key=lambda r: r.canonical()):
            attributing = sorted(
                {e.left_table.canonical() for e in grouped[sink] if is_attribution(e, cfg)}
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
