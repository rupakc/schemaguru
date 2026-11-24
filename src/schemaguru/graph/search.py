"""Path search over the measured join graph (spec 9.3, 9.4, 9.6).

* Dijkstra over **both** directions of every edge (a parent hub has no out-edges).
* State key ``(table, waypoint_automaton_state, hops, arrived_on_attribution,
  hubs_entered_bitmask)``: the hop count is part of the key so a cheap long route cannot block
  a short one under the hop bound.
* Paths are simple (no table twice); a hub is never entered twice.
* Two passes: pass 1 prunes every hop whose known support is below ``thin_support``; only if it
  finds nothing, pass 2 drops that pruning and its paths are ``provisional``.
* ``k_paths`` best paths by Yen's algorithm.
* No path: typed refusal; diagnostic re-runs relax exactly one mechanism to name the blocker,
  and the relaxed route is never returned.
"""

from __future__ import annotations

import heapq
import math
from collections.abc import Hashable, Iterable, Sequence
from dataclasses import dataclass
from enum import Enum
from itertools import pairwise
from typing import Any, Protocol

from schemaguru.config import GraphConfig
from schemaguru.graph.cost import hop_cost, hop_support
from schemaguru.graph.edges import Edge
from schemaguru.graph.hubs import Hubs, is_attribution
from schemaguru.model.refs import ObjectRef


@dataclass(frozen=True, eq=False)
class Hop:
    """One traversal of ``edge``: child -> parent, or parent -> child when ``reverse``."""

    edge: Edge
    reverse: bool

    @property
    def start(self) -> ObjectRef:
        return self.edge.right_table if self.reverse else self.edge.left_table

    @property
    def end(self) -> ObjectRef:
        return self.edge.left_table if self.reverse else self.edge.right_table

    @property
    def key(self) -> tuple[str, bool]:
        return (self.edge.key, self.reverse)

    @property
    def support(self) -> float | None:
        return hop_support(self.edge, reverse=self.reverse)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Hop) and other.key == self.key

    def __hash__(self) -> int:
        return hash(self.key)

    def __repr__(self) -> str:
        arrow = "<-" if self.reverse else "->"
        return f"Hop({self.start.canonical()} {arrow} {self.end.canonical()})"


@dataclass(frozen=True)
class Path:
    hops: tuple[Hop, ...]
    cost: float
    coverage: float | None  # narrowest support over hops; None if any hop's support is unknown
    provisional: bool
    estimated: bool = False  # some hop's numbers come from a sample

    @property
    def tables(self) -> tuple[ObjectRef, ...]:
        return (self.hops[0].start, *(h.end for h in self.hops)) if self.hops else ()


class Refusal(Enum):
    NO_EVIDENCE = "NO_EVIDENCE"
    SAME_TABLE = "SAME_TABLE"
    NO_ROUTE = "NO_ROUTE"
    WAYPOINT_UNSATISFIABLE = "WAYPOINT_UNSATISFIABLE"
    RULE_REJECTS_ALL = "RULE_REJECTS_ALL"
    HUB_PIVOT = "HUB_PIVOT"


@dataclass(frozen=True)
class PathResult:
    paths: tuple[Path, ...]
    refusal: Refusal | None
    explanation: str


class Automaton(Protocol):
    """Waypoint automaton (spec 9.5; compiled by ``graph.waypoints``).

    ``start`` is the state before the first hop; ``step`` must be pure and its states hashable.
    """

    @property
    def start(self) -> Hashable: ...

    def step(self, state: Any, hop: Hop) -> Any: ...

    def accepting(self, state: Any) -> bool: ...

    def is_empty(self) -> bool: ...


class Graph:
    """Edges plus hubs, with adjacency built lazily one connected component at a time."""

    def __init__(self, edges: Sequence[Edge], hubs: Hubs) -> None:
        self.edges: tuple[Edge, ...] = tuple(sorted(edges, key=lambda e: e.key))
        self.hubs = hubs
        self._in_degree: dict[ObjectRef, int] = {}
        parent: dict[ObjectRef, ObjectRef] = {}

        def find(x: ObjectRef) -> ObjectRef:
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for e in self.edges:
            a, b = e.left_table, e.right_table
            self._in_degree[b] = self._in_degree.get(b, 0) + 1
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[max(ra, rb, key=lambda r: r.canonical())] = min(
                    ra, rb, key=lambda r: r.canonical()
                )
        self.tables: frozenset[ObjectRef] = frozenset(parent)
        self._component: dict[ObjectRef, ObjectRef] = {t: find(t) for t in parent}
        self._adjacency: dict[ObjectRef, dict[ObjectRef, tuple[Hop, ...]]] = {}
        hub_order = sorted(hubs.tables, key=lambda r: r.canonical())
        self._hub_bit: dict[ObjectRef, int] = {h: 1 << i for i, h in enumerate(hub_order)}

    @classmethod
    def build(cls, edges: Iterable[Edge], hubs: Hubs) -> Graph:
        return cls(list(edges), hubs)

    def in_degree(self, table: ObjectRef) -> int:
        return self._in_degree.get(table, 0)

    def component(self, table: ObjectRef) -> ObjectRef | None:
        return self._component.get(table)

    def built_components(self) -> int:
        return len(self._adjacency)

    def hub_bit(self, table: ObjectRef) -> int:
        return self._hub_bit.get(table, 0)

    def neighbours(self, table: ObjectRef) -> tuple[Hop, ...]:
        comp = self._component.get(table)
        if comp is None:
            return ()
        adj = self._adjacency.get(comp)
        if adj is None:
            lists: dict[ObjectRef, list[Hop]] = {}
            for e in self.edges:
                if self._component[e.left_table] != comp:
                    continue
                for hop in (Hop(e, False), Hop(e, True)):
                    lists.setdefault(hop.start, []).append(hop)
            adj = {
                t: tuple(sorted(hs, key=lambda h: (h.end.canonical(), h.edge.key, h.reverse)))
                for t, hs in lists.items()
            }
            self._adjacency[comp] = adj
        return adj.get(table, ())


# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Mode:
    prune_thin: bool
    hub_guard: bool
    hop_limit: int
    automaton: Automaton | None


_Key = tuple[ObjectRef, Any, int, bool, int]


def _hop_cost(graph: Graph, hop: Hop, cfg: GraphConfig) -> float:
    return hop_cost(hop.edge, reverse=hop.reverse, in_degree=graph.in_degree(hop.end), cfg=cfg)


def _replay(
    graph: Graph, src: ObjectRef, hops: Sequence[Hop], cfg: GraphConfig, mode: _Mode
) -> _Key:
    """The search state after ``hops`` from ``src`` (for Yen's spur searches)."""
    a = mode.automaton
    state: Any = None if a is None else a.start
    attr, mask, table = False, graph.hub_bit(src), src
    for h in hops:
        state = None if a is None else a.step(state, h)
        attr, mask, table = is_attribution(h.edge, cfg), mask | graph.hub_bit(h.end), h.end
    return (table, state, len(hops), attr, mask)


def _dijkstra(
    graph: Graph,
    start: _Key,
    dst: ObjectRef,
    cfg: GraphConfig,
    mode: _Mode,
    *,
    banned_hops: frozenset[tuple[str, bool]] = frozenset(),
    banned_tables: frozenset[ObjectRef] = frozenset(),
) -> list[Hop] | None:
    a = mode.automaton
    best: dict[_Key, float] = {start: 0.0}
    back: dict[_Key, tuple[_Key, Hop]] = {}
    done: set[_Key] = set()
    counter = 0
    heap: list[tuple[float, int, int, _Key]] = [(0.0, start[2], counter, start)]

    def on_path(key: _Key, table: ObjectRef) -> bool:
        k: _Key | None = key
        while k is not None:
            if k[0] == table:
                return True
            prev = back.get(k)
            k = None if prev is None else prev[0]
        return False

    while heap:
        cost, _, _, key = heapq.heappop(heap)
        if key in done:
            continue
        done.add(key)
        table, state, hops, attr, mask = key
        if table == dst:
            if a is None or a.accepting(state):
                out: list[Hop] = []
                k = key
                while k in back:
                    k, hop = back[k]
                    out.append(hop)
                return out[::-1]
            continue  # never pass through the destination
        if hops >= mode.hop_limit:
            continue
        leaving_hub = mode.hub_guard and attr and graph.hubs.is_hub(table)
        for hop in graph.neighbours(table):
            nxt = hop.end
            if hop.key in banned_hops or nxt in banned_tables:
                continue
            s = hop.support
            if mode.prune_thin and s is not None and s < cfg.thin_support:
                continue
            arrive_attr = is_attribution(hop.edge, cfg)
            if leaving_hub and arrive_attr:
                continue  # cartesian hazard: attribution in, attribution out
            bit = graph.hub_bit(nxt)
            if bit & mask or on_path(key, nxt):
                continue
            c = _hop_cost(graph, hop, cfg)
            if math.isinf(c):
                continue
            nkey: _Key = (
                nxt,
                None if a is None else a.step(state, hop),
                hops + 1,
                arrive_attr,
                mask | bit,
            )
            nc = cost + c
            if nkey not in done and nc < best.get(nkey, math.inf):
                best[nkey] = nc
                back[nkey] = (key, hop)
                counter += 1
                heapq.heappush(heap, (nc, hops + 1, counter, nkey))
    return None


def _route_key(hops: Sequence[Hop]) -> tuple[tuple[str, bool], ...]:
    return tuple(h.key for h in hops)


def _total(graph: Graph, hops: Sequence[Hop], cfg: GraphConfig) -> float:
    return sum(_hop_cost(graph, h, cfg) for h in hops)


def _yen(
    graph: Graph, src: ObjectRef, dst: ObjectRef, cfg: GraphConfig, mode: _Mode, k: int
) -> list[tuple[float, list[Hop]]]:
    first = _dijkstra(graph, _replay(graph, src, (), cfg, mode), dst, cfg, mode)
    if first is None or k < 1:
        return []
    found: list[tuple[float, list[Hop]]] = [(_total(graph, first, cfg), first)]
    seen = {_route_key(first)}
    pool: dict[tuple[tuple[str, bool], ...], tuple[float, list[Hop]]] = {}
    while len(found) < k:
        prev = found[-1][1]
        for i in range(len(prev)):
            root = prev[:i]
            spur = root[-1].end if root else src
            rkey = _route_key(root)
            banned_hops = frozenset(
                p[i].key for _, p in found if len(p) > i and _route_key(p[:i]) == rkey
            )
            banned_tables = frozenset({src, *(h.end for h in root)} - {spur})
            tail = _dijkstra(
                graph,
                _replay(graph, src, root, cfg, mode),
                dst,
                cfg,
                mode,
                banned_hops=banned_hops,
                banned_tables=banned_tables,
            )
            if tail is None:
                continue
            route = [*root, *tail]
            key = _route_key(route)
            if key not in seen and key not in pool:
                pool[key] = (_total(graph, route, cfg), route)
        if not pool:
            break
        pick = min(pool, key=lambda r: (pool[r][0], len(r), repr(r)))
        found.append(pool.pop(pick))
        seen.add(pick)
    return found


def _path(cost: float, hops: Sequence[Hop], provisional: bool) -> Path:
    supports = [h.support for h in hops]
    coverage = (
        None if any(s is None for s in supports) else min(s for s in supports if s is not None)
    )
    estimated = any(h.edge.estimated for h in hops)
    return Path(tuple(hops), cost, coverage, provisional, estimated)


def _name(t: ObjectRef) -> str:
    return t.canonical()


def _diagnose(
    graph: Graph, src: ObjectRef, dst: ObjectRef, cfg: GraphConfig, automaton: Automaton | None
) -> PathResult:
    """Relax exactly one mechanism at a time; the relaxed route only names the blocker."""
    pair = f"{_name(src)} -> {_name(dst)}"

    def probe(mode: _Mode) -> list[Hop] | None:
        return _dijkstra(graph, _replay(graph, src, (), cfg, mode), dst, cfg, mode)

    if automaton is not None and probe(_Mode(False, True, cfg.hop_limit, None)) is not None:
        return PathResult(
            (),
            Refusal.RULE_REJECTS_ALL,
            f"waypoint rules reject every route {pair}; without them a route exists",
        )
    relaxed = probe(_Mode(False, False, cfg.hop_limit, automaton))
    if relaxed is not None:
        pivots = sorted(
            {
                _name(h.end)
                for h, nxt in pairwise(relaxed)
                if graph.hubs.is_hub(h.end)
                and is_attribution(h.edge, cfg)
                and is_attribution(nxt.edge, cfg)
            }
        )
        return PathResult(
            (),
            Refusal.HUB_PIVOT,
            f"every route {pair} pivots through hub {', '.join(pivots)} on two attribution "
            f"edges (fan-in > {cfg.attribution_fanin:g}): a cartesian hazard",
        )
    unbounded = probe(_Mode(False, True, max(len(graph.tables), 1), automaton))
    if unbounded is not None:
        return PathResult(
            (),
            Refusal.NO_ROUTE,
            f"no route {pair} within the hop limit {cfg.hop_limit}; the shortest needs "
            f"{len(unbounded)} hops",
        )
    return PathResult(
        (),
        Refusal.NO_ROUTE,
        f"no route {pair}: no single relaxation (rules, hub guard, hop limit) opens one",
    )


def find_paths(
    graph: Graph,
    src: ObjectRef,
    dst: ObjectRef,
    *,
    cfg: GraphConfig,
    automaton: Automaton | None = None,
) -> PathResult:
    """Up to ``cfg.k_paths`` cheapest routes ``src`` -> ``dst`` (tables; columns are widened)."""
    s, d = src.table_ref(), dst.table_ref()
    if s == d:
        return PathResult((), Refusal.SAME_TABLE, f"{_name(s)} is both ends; no join is needed")
    missing = [_name(t) for t in (s, d) if t not in graph.tables]
    if missing:
        return PathResult(
            (),
            Refusal.NO_EVIDENCE,
            f"no usable measured join touches {', '.join(missing)} "
            "(broken joins are refused; unmeasured candidates need include_unmeasured)",
        )
    if automaton is not None and automaton.is_empty():
        return PathResult(
            (),
            Refusal.WAYPOINT_UNSATISFIABLE,
            "the waypoint rules for this pair can never be satisfied",
        )
    if graph.component(s) != graph.component(d):
        return PathResult(
            (),
            Refusal.NO_ROUTE,
            f"no chain of usable joins connects {_name(s)} and {_name(d)}",
        )
    for prune in (True, False):
        mode = _Mode(prune, True, cfg.hop_limit, automaton)
        found = _yen(graph, s, d, cfg, mode, cfg.k_paths)
        if found:
            paths = tuple(_path(c, hops, provisional=not prune) for c, hops in found)
            note = ""
            if not prune:
                narrowest = min((p.coverage for p in paths if p.coverage is not None), default=None)
                shown = "unknown" if narrowest is None else f"{narrowest:.4g}"
                note = (
                    f"provisional: every route has a hop with support below "
                    f"{cfg.thin_support:g}; narrowest support {shown}"
                )
            return PathResult(paths, None, note)
    return _diagnose(graph, s, d, cfg, automaton)
