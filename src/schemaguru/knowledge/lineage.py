"""Lineage traversal (0.2.0 spec 4.8): what a table or column is built from, or feeds.

Breadth-first over EFFECTIVE lineage (:func:`~schemaguru.model.lineage.resolve_lineage`), with
at most two store calls per depth. Upstream reads the frontier's own ``LINEAGE`` /
``COLUMN_LINEAGE`` facts, so any :class:`~schemaguru.store.protocol.Store` serves it; downstream
needs the store's lineage index (``SG705`` without one). A node is expanded at most once; an
edge into a node that reaches back to its source (an ancestor or a sibling) is reported as a
cycle, never followed; the walk stops at ``max_nodes``. Lineage is declared or parsed, never
measured, and never a join path.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from schemaguru.errors import SchemaGuruError
from schemaguru.model.kinds import LINEAGE_KINDS
from schemaguru.model.lineage import Level, LineageLink, resolve_lineage
from schemaguru.model.refs import ObjectRef
from schemaguru.store.protocol import Store, lineage_index

MAX_NODES = 200
MAX_DEPTH = 5
Direction = Literal["upstream", "downstream"]


@dataclass(frozen=True)
class LineageNode:
    """A table or column reached by the walk; ``in_catalog`` is False for a name lineage
    mentions that resolved to no stored relation (a leaf)."""

    ref: str
    level: Level
    depth: int
    in_catalog: bool


@dataclass(frozen=True)
class LineageEdgeView:
    """``downstream`` is built from ``upstream``; ``sources`` name where that was read
    (``dbt:manifest``, ``view_definition``, an overlay); ``transform`` is column-level only."""

    upstream: str
    downstream: str
    sources: tuple[str, ...]
    transform: str | None


@dataclass(frozen=True)
class LineageResult:
    root: str
    direction: Direction
    level: Level
    depth: int
    nodes: tuple[LineageNode, ...]
    edges: tuple[LineageEdgeView, ...]
    truncated: bool
    cycles: tuple[tuple[str, ...], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.root,
            "direction": self.direction,
            "level": self.level,
            "depth": self.depth,
            "nodes": [
                {"ref": n.ref, "level": n.level, "depth": n.depth, "in_catalog": n.in_catalog}
                for n in self.nodes
            ],
            "edges": [
                {
                    "upstream": e.upstream,
                    "downstream": e.downstream,
                    "source": list(e.sources),
                    "transform": e.transform,
                }
                for e in self.edges
            ],
            "truncated": self.truncated,
            "cycles": [list(c) for c in self.cycles],
        }


# (neighbour, transform) -> sources: one hop out of a frontier node
_Hops = dict[tuple[str, str | None], set[str]]


def _parse(key: str, level: Level) -> ObjectRef | None:
    try:
        return ObjectRef.parse(key, has_column=level == "column")
    except ValueError:
        return None


def _links(store: Store, keys: Sequence[str], level: Level) -> list[LineageLink]:
    refs = [r for r in (_parse(k, level) for k in keys) if r is not None]
    if not refs:
        return []
    links = resolve_lineage(store.facts_for(refs, sorted(LINEAGE_KINDS))).links
    return [lk for lk in links if lk.level == level]


def _add(hops: dict[str, _Hops], at: str, to: str, link: LineageLink) -> None:
    hops.setdefault(at, {}).setdefault((to, link.transform), set()).update(link.sources)


def _upstream(
    store: Store, frontier: Sequence[str], level: Level
) -> tuple[dict[str, _Hops], set[str]]:
    hops: dict[str, _Hops] = {}
    unresolved: set[str] = set()
    for link in _links(store, frontier, level):
        for name in link.inputs:
            _add(hops, link.subject, name, link)
        for name in link.unresolved:
            _add(hops, link.subject, name, link)
            unresolved.add(name)
    return hops, unresolved


def _downstream(
    store: Store, frontier: Sequence[str], level: Level
) -> tuple[dict[str, _Hops], set[str]]:
    index = lineage_index(store)
    if index is None:
        raise SchemaGuruError(
            "SG705 the store does not implement the lineage index: downstream lineage is "
            "unavailable (upstream works)"
        )
    edges = index.lineage_edges(list(frontier), direction="downstream", level=level)
    wanted = {(e.upstream_key, e.downstream_key) for e in edges}
    hops: dict[str, _Hops] = {}
    for link in _links(store, sorted({e.downstream_key for e in edges}), level):
        for name in link.inputs:
            if (name, link.subject) in wanted:
                _add(hops, name, link.subject, link)
    return hops, set()


def _path(walk: dict[str, list[str]], start: str, goal: str) -> tuple[str, ...] | None:
    """The shortest path ``start -> ... -> goal`` over the walk's edges so far (breadth-first,
    so bounded by the node cap), or None when ``goal`` is not reachable."""
    prev: dict[str, str | None] = {start: None}
    queue = [start]
    for cur in queue:
        if cur == goal:
            chain = [cur]
            while (p := prev[chain[-1]]) is not None:
                chain.append(p)
            return tuple(reversed(chain))
        for nxt in walk.get(cur, ()):
            if nxt not in prev:
                prev[nxt] = cur
                queue.append(nxt)
    return None


def traverse(
    store: Store,
    root: ObjectRef,
    *,
    direction: Direction = "upstream",
    depth: int = 2,
    level: Level | None = None,
    max_nodes: int = MAX_NODES,
) -> LineageResult:
    """The lineage of ``root`` up to ``depth`` hops (1..5); ``level`` defaults to ``column``
    for a column ref and ``table`` otherwise (``table`` on a column ref walks its table)."""
    if isinstance(depth, bool) or not isinstance(depth, int) or not 1 <= depth <= MAX_DEPTH:
        raise ValueError(f"depth must be an integer in 1..{MAX_DEPTH}: {depth!r}")
    if direction not in ("upstream", "downstream"):
        raise ValueError(f"direction must be 'upstream' or 'downstream': {direction!r}")
    if level not in (None, "table", "column"):
        raise ValueError(f"level must be 'table' or 'column': {level!r}")
    lvl: Level = level or ("column" if root.column is not None else "table")
    if lvl == "column" and root.column is None:
        raise ValueError(f"column-level lineage needs a column, not {root.canonical()}")
    start = root.canonical() if lvl == "column" else root.table_ref().canonical()
    step = _upstream if direction == "upstream" else _downstream

    nodes: dict[str, LineageNode] = {start: LineageNode(start, lvl, 0, True)}
    walk: dict[str, list[str]] = {}  # the edges followed so far, in the walk's direction
    edges: list[LineageEdgeView] = []
    cycles: list[tuple[str, ...]] = []
    truncated = False
    frontier = [start]
    for d in range(1, depth + 1):
        if not frontier or truncated:
            break
        hops, unresolved = step(store, frontier, lvl)
        nxt: list[str] = []
        for at in frontier:
            for (to, transform), sources in sorted(
                hops.get(at, {}).items(), key=lambda kv: (kv[0][0], kv[0][1] or "")
            ):
                if to not in nodes:
                    if len(nodes) >= max_nodes:
                        truncated = True
                        break
                    leaf = to in unresolved
                    nodes[to] = LineageNode(to, lvl, d, not leaf)
                    if not leaf:
                        nxt.append(to)
                else:
                    # an edge into a node already seen closes a cycle when that node reaches
                    # back to ``at`` (an ancestor, or a sibling that feeds it)
                    cycle = _path(walk, to, at)
                    if cycle is not None:
                        cycles.append((*cycle, to))
                walk.setdefault(at, []).append(to)
                up, down = (to, at) if direction == "upstream" else (at, to)
                edges.append(LineageEdgeView(up, down, tuple(sorted(sources)), transform))
            if truncated:
                break
        frontier = nxt
    return LineageResult(
        root=start,
        direction=direction,
        level=lvl,
        depth=depth,
        nodes=tuple(nodes.values()),
        edges=tuple(edges),
        truncated=truncated,
        cycles=tuple(_unique(cycles)),
    )


def _unique(items: Iterable[tuple[str, ...]]) -> list[tuple[str, ...]]:
    return list(dict.fromkeys(items))


__all__ = [
    "MAX_DEPTH",
    "MAX_NODES",
    "Direction",
    "LineageEdgeView",
    "LineageNode",
    "LineageResult",
    "traverse",
]
