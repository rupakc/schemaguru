"""Path search over the measured join graph (spec 9.3, 9.4, 9.6).

* Dijkstra over **both** directions of every edge (a parent hub has no out-edges).
* State key ``(table, waypoint_automaton_state, hops, arrived_on_attribution,
  hubs_entered_bitmask)``: the hop count is part of the key so a cheap long route cannot block
  a short one under the hop bound.
* No visited-set pruning (it would make the label-setting search incomplete): a table is
  revisited only when the waypoint rules force it (``Path.revisits``); a hub is never entered
  twice.
* The destination is passed through while the rules are not yet satisfied; a route ends on
  arriving at it in an accepting state.
* Two passes: pass 1 prunes every hop whose known support is below ``thin_support``; pass 2 drops
  that pruning, requires a thin hop (a bit in its state) and its up to ``k_paths`` routes are
  appended after pass 1's, ``provisional``.
* ``k_paths`` best paths by Yen's algorithm over the product graph (table, automaton state):
  simple in that graph, so a rule-forced revisit is found as readily as a simple route.
* No path: typed refusal; diagnostic re-runs relax exactly one mechanism to name the blocker,
  and the relaxed route is never returned.
"""

from __future__ import annotations

import heapq
import math
import threading
from collections.abc import Collection, Hashable, Iterable, Sequence
from dataclasses import dataclass
from enum import Enum
from itertools import pairwise
from typing import Any, Protocol

from schemaguru.config import GraphConfig
from schemaguru.graph.cost import hop_cost, hop_support
from schemaguru.graph.edges import Edge
from schemaguru.graph.hubs import Hubs, is_attribution
from schemaguru.model.refs import ObjectRef


class Hop:
    """One traversal of ``edge``: child -> parent, or parent -> child when ``reverse``.

    Immutable by convention. ``start``, ``end``, ``key`` and ``support`` are computed once: the
    search reads them constantly, and a slotted class builds 100k hops several times faster
    than a frozen dataclass.
    """

    __slots__ = ("_hash", "edge", "end", "key", "reverse", "start", "support")

    def __init__(self, edge: Edge, reverse: bool) -> None:
        self.edge = edge
        self.reverse = reverse
        if reverse:
            self.start, self.end = edge.right_table, edge.left_table
        else:
            self.start, self.end = edge.left_table, edge.right_table
        self.key: tuple[str, bool] = (edge.key, reverse)
        self.support: float | None = hop_support(edge, reverse=reverse)
        self._hash = hash(self.key)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Hop) and other.key == self.key

    def __hash__(self) -> int:
        return self._hash

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
    # a table appears twice: only when waypoint rules force it; rendered as an aliased self-join
    revisits: bool = False

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


_Arc = tuple["Hop", int, int, int]


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

        canon: dict[ObjectRef, str] = {}

        def name(r: ObjectRef) -> str:
            c = canon.get(r)
            if c is None:
                c = canon[r] = r.canonical()
            return c

        worst: float | None = None
        for e in self.edges:
            a, b = e.left_table, e.right_table
            self._in_degree[b] = self._in_degree.get(b, 0) + 1
            ra, rb = find(a), find(b)
            if ra != rb:
                lo, hi = (ra, rb) if name(ra) <= name(rb) else (rb, ra)
                parent[hi] = lo
            dl = e.value.get("distinct_left") if e.measured else None
            if dl is not None and (worst is None or float(dl) > worst):
                worst = float(dl)
        self.tables: frozenset[ObjectRef] = frozenset(parent)
        self._component: dict[ObjectRef, ObjectRef] = {t: find(t) for t in parent}
        # Search works on integer table ids (ObjectRef hashing dominated the profile).
        refs = sorted(parent, key=name)
        self._ids: dict[ObjectRef, int] = {r: i for i, r in enumerate(refs)}
        self._refs: list[ObjectRef] = refs
        self._arcs: dict[int, tuple[_Arc, ...]] = {}
        self._into: dict[int, list[tuple[int, Hop, int]]] = {}  # end id -> (start id, hop, idx)
        self._n_hops = 0
        self._ends = [(self._ids[e.left_table], self._ids[e.right_table]) for e in self.edges]
        root_ids = {r: self._ids[r] for r in set(self._component.values())}
        self._comp_of = [root_ids[self._component[r]] for r in refs]
        self._built: set[int] = set()
        self._hops: list[tuple[Hop, int]] = []  # by hop index: (hop, end id)
        self._indeg_by_id = [self._in_degree.get(r, 0) for r in refs]
        # per-config memo of hop costs and lower bounds, reused across calls (bounded)
        self._ctxs: dict[GraphConfig, _Ctx] = {}
        # one lock guards every lazy structure (components, memos): a server shares one Graph
        self._lock = threading.RLock()
        self._thin: dict[float, frozenset[ObjectRef]] = {}
        # largest known distinct_left: the price of an unknown fan-out (spec 9.3)
        self.worst_fanout: float | None = worst
        hub_order = sorted(hubs.tables, key=lambda r: r.canonical())
        self._hub_bit: dict[ObjectRef, int] = {h: 1 << i for i, h in enumerate(hub_order)}
        self._bits_by_id = [self._hub_bit.get(r, 0) for r in refs]
        self._hub_ids = frozenset(self._ids[h] for h in hub_order if h in self._ids)

    @classmethod
    def build(cls, edges: Iterable[Edge], hubs: Hubs) -> Graph:
        return cls(list(edges), hubs)

    def in_degree(self, table: ObjectRef) -> int:
        return self._in_degree.get(table, 0)

    def component(self, table: ObjectRef) -> ObjectRef | None:
        return self._component.get(table)

    def built_components(self) -> int:
        return len(self._built)

    def hub_bit(self, table: ObjectRef) -> int:
        return self._hub_bit.get(table, 0)

    def hub_ids(self) -> frozenset[int]:
        return self._hub_ids

    def has_thin(self, table: ObjectRef, thin: float) -> bool:
        """Whether ``table``'s component has a hop with known support below ``thin``."""
        comps = self._thin.get(thin)
        if comps is None:
            comps = frozenset(
                self._component[e.left_table]
                for e in self.edges
                for rev in (False, True)
                if (sup := hop_support(e, reverse=rev)) is not None and sup < thin
            )
            with self._lock:
                comps = self._thin.setdefault(thin, comps)
        return self._component.get(table) in comps

    def hop_count(self) -> int:
        return self._n_hops

    def table_id(self, table: ObjectRef) -> int | None:
        return self._ids.get(table)

    def into(self, tid: int) -> list[tuple[int, Hop, int]]:
        """Internal: hops arriving at table ``tid`` (its component must be built)."""
        return self._into.get(tid, [])

    def arcs(self, tid: int) -> tuple[_Arc, ...]:
        """Internal: ``(hop, end id, end hub bit, hop index)`` leaving table ``tid``."""
        arcs = self._arcs.get(tid)
        if arcs is None:
            self._build(tid)
            arcs = self._arcs.get(tid, ())
        return arcs

    def neighbours(self, table: ObjectRef) -> tuple[Hop, ...]:
        """Hops leaving ``table``, both directions, in a deterministic order."""
        tid = self._ids.get(table)
        return () if tid is None else tuple(a[0] for a in self.arcs(tid))

    def _build(self, tid: int) -> None:
        with self._lock:
            self._build_locked(tid)

    def _build_locked(self, tid: int) -> None:
        comp = self._comp_of[tid]
        if comp in self._built:
            return
        comp_of, bits_of = self._comp_of, self._bits_by_id
        lists: dict[int, list[tuple[int, str, bool, Hop]]] = {}
        for e, (lo, ro) in zip(self.edges, self._ends, strict=True):
            if comp_of[lo] != comp:
                continue
            lists.setdefault(lo, []).append((ro, e.key, False, Hop(e, False)))
            lists.setdefault(ro, []).append((lo, e.key, True, Hop(e, True)))
        for start, entries in lists.items():
            entries.sort(key=lambda x: (x[0], x[1], x[2]))  # ids follow canonical order
            arcs: list[_Arc] = []
            for end, _, _, h in entries:
                arc = (h, end, bits_of[end], self._n_hops)
                arcs.append(arc)
                self._into.setdefault(end, []).append((start, h, arc[3]))
                self._n_hops += 1
                self._hops.append((h, end))
            self._arcs[start] = tuple(arcs)
        self._built.add(comp)  # only once every arc of the component is in place


# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Mode:
    prune_thin: bool  # pass 1: never take a hop with known support below thin_support
    hub_guard: bool
    hop_limit: int
    automaton: Automaton | None
    require_thin: bool = False  # pass 2: accept only routes that took a thin hop


# (table id, automaton state, hops, arrived on attribution, hubs entered, thin hop taken).
# The first five are spec 9.3's key; the thin bit is always False outside pass 2.
_Key = tuple[int, Any, int, bool, int, bool]
# A node of the product graph Yen works on: (table id, automaton state, thin hop taken).
_Node = tuple[int, Any, bool]


def _node(key: _Key) -> _Node:
    return (key[0], key[1], key[5])


_MAX_CONFIGS = 4
_MAX_LOWER_BOUNDS = 64


class _Ctx:
    """Memoised hop costs, attribution flags and lower bounds for one (graph, config).

    The graph is immutable and the config is frozen, so the memo is reused across calls
    (``_Ctx.of``); weights are still read from the config the caller passes, at call time.
    """

    @classmethod
    def of(cls, graph: Graph, cfg: GraphConfig) -> _Ctx:
        ctxs = graph._ctxs
        ctx = ctxs.get(cfg)
        if ctx is None:
            with graph._lock:
                ctx = ctxs.get(cfg)
                if ctx is None:
                    if len(ctxs) >= _MAX_CONFIGS:
                        del ctxs[next(iter(ctxs))]
                    ctx = ctxs[cfg] = cls(graph, cfg)
        return ctx

    def __init__(self, graph: Graph, cfg: GraphConfig) -> None:
        self.graph, self.cfg = graph, cfg
        self._cost: dict[Hop, float] = {}
        self._attr: dict[str, bool] = {}
        self._cost_i: list[float | None] = []
        self._lower: dict[tuple[int, ...], list[float]] = {}
        self._attr_i: list[bool | None] = []

    def cost(self, hop: Hop) -> float:
        c = self._cost.get(hop)
        if c is None:
            g = self.graph
            c = hop_cost(
                hop.edge,
                reverse=hop.reverse,
                in_degree=g.in_degree(hop.end),
                cfg=self.cfg,
                worst_fanout=g.worst_fanout,
            )
            self._cost[hop] = c
        return c

    def lower_bound(self, *dsts: int) -> list[float]:
        """Cheapest cost from every table to the nearest of ``dsts``, every constraint relaxed.

        Admissible and consistent (a shortest-path metric over the same hop costs, 0 at every
        target), so A* with it still settles each target's exact optimum of the constrained search.
        """
        cache_key = tuple(sorted(set(dsts)))
        h = self._lower.get(cache_key)
        if h is not None:
            return h
        g = self.graph
        with g._lock:  # builds components and sizes the cost memo: one thread at a time
            h = self._lower.get(cache_key)
            if h is None:
                h = self._lower_bound_locked(cache_key)
        return h

    def _lower_bound_locked(self, cache_key: tuple[int, ...]) -> list[float]:
        g = self.graph
        for t in cache_key:
            g.arcs(t)  # builds the component
        costs = self._fill_costs()
        h = [math.inf] * len(g._refs)
        heap: list[tuple[float, int]] = []
        for t in cache_key:
            h[t] = 0.0
            heap.append((0.0, t))
        while heap:
            d, v = heapq.heappop(heap)
            if d > h[v]:
                continue
            for u, hop, idx in g.into(v):
                c = costs[idx]
                if c is None:
                    c = costs[idx] = self.cost(hop)
                nd = d + c
                if nd < h[u]:
                    h[u] = nd
                    heapq.heappush(heap, (nd, u))
        if len(self._lower) >= _MAX_LOWER_BOUNDS:
            del self._lower[next(iter(self._lower))]  # oldest first
        self._lower[cache_key] = h
        return h

    def _fill_costs(self) -> list[float | None]:
        """Every built hop's cost in one tight loop (same formula as :func:`hop_cost`)."""
        costs, _ = self.tables_by_hop()
        cfg, g = self.cfg, self.graph
        ws, wf, wh, pen = cfg.w_support, cfg.w_fanout, cfg.w_hub, cfg.unmeasured_penalty
        worst = g.worst_fanout
        unknown_fan = wf * (pen if worst is None else math.log1p(worst))
        indeg = g._indeg_by_id
        log, log1p, inf = math.log, math.log1p, math.inf
        for idx, (hop, end) in enumerate(g._hops):
            if costs[idx] is not None:
                continue
            e = hop.edge
            sup = hop.support
            if sup is None:
                c = pen
            elif sup <= 0.0:
                costs[idx] = inf
                continue
            else:
                c = ws * -log(min(sup, 1.0))
            dl = e.value.get("distinct_left") if e.measured else None
            c += unknown_fan if dl is None else wf * log1p(float(dl))
            costs[idx] = c + wh * log1p(max(indeg[end], 0))
        return costs

    def tables_by_hop(self) -> tuple[list[float | None], list[bool | None]]:
        """Per-hop-index memo lists (cost, attribution), grown to the graph's built hops."""
        n = self.graph.hop_count()
        with self.graph._lock:
            for memo in (self._cost_i, self._attr_i):
                if len(memo) < n:
                    memo.extend([None] * (n - len(memo)))
        return self._cost_i, self._attr_i

    def attribution(self, edge: Edge) -> bool:
        a = self._attr.get(edge.key)
        if a is None:
            a = self._attr[edge.key] = is_attribution(edge, self.cfg)
        return a

    def total(self, hops: Sequence[Hop]) -> float:
        return sum(self.cost(h) for h in hops)


def _replay(ctx: _Ctx, src: ObjectRef, hops: Sequence[Hop], mode: _Mode) -> list[_Key]:
    """Every search state along ``hops`` from ``src`` (``len(hops) + 1`` keys)."""
    a, graph = mode.automaton, ctx.graph
    thin = ctx.cfg.thin_support
    state: Any = None if a is None else a.start
    attr, mask, used = False, graph.hub_bit(src), False
    tid = graph.table_id(src)
    assert tid is not None
    keys: list[_Key] = [(tid, state, 0, attr, mask, used)]
    for n, h in enumerate(hops, 1):
        state = None if a is None else a.step(state, h)
        attr, mask = ctx.attribution(h.edge), mask | graph.hub_bit(h.end)
        if mode.require_thin and h.support is not None and h.support < thin:
            used = True
        end = graph.table_id(h.end)
        assert end is not None  # replayed hops come from this graph
        keys.append((end, state, n, attr, mask, used))
    return keys


def _dijkstra(
    ctx: _Ctx,
    start: _Key,
    dst: ObjectRef,
    mode: _Mode,
    *,
    spur_banned: frozenset[tuple[str, bool]] = frozenset(),
    banned_nodes: frozenset[_Node] = frozenset(),
    bound: float = math.inf,
) -> list[Hop] | None:
    dst_id = ctx.graph.table_id(dst)
    assert dst_id is not None
    found = _search(
        ctx,
        start,
        frozenset({dst_id}),
        mode,
        multi=False,
        spur_banned=spur_banned,
        banned_nodes=banned_nodes,
        bound=bound,
    )
    return found.get(dst_id)


def _search(
    ctx: _Ctx,
    start: _Key,
    targets: frozenset[int],
    mode: _Mode,
    *,
    multi: bool,
    spur_banned: frozenset[tuple[str, bool]] = frozenset(),
    banned_nodes: frozenset[_Node] = frozenset(),
    bound: float = math.inf,
) -> dict[int, list[Hop]]:
    """A* over search states; the key is exactly spec 9.3's (plus pass 2's thin bit).

    There is no visited-set pruning: a table may be revisited, which on a cheapest route happens
    only when the waypoint automaton forces it. Hubs are never entered twice. The destination is
    passed through while the automaton is not yet accepting; a route ends on arriving at the
    destination in an accepting state. A label is dominated (skipped) by a settled label at the
    same (table, automaton state, thin bit) that is no more constrained: no more hops, a subset
    of its hubs entered, and not arrived on attribution unless it did too. Every completion of
    the dominated label is open to the settled one at the same added cost, and A* settles a
    table in cost order, so the settled label is never dearer.

    Yen hooks: ``spur_banned`` hops are refused only when leaving ``start``; ``banned_nodes``
    are product nodes (table, automaton state, thin bit) never arrived at (the root path).
    """
    graph, a = ctx.graph, mode.automaton
    hub_ids = graph.hub_ids()
    thin = ctx.cfg.thin_support
    prune = mode.prune_thin
    need_thin = mode.require_thin
    limit = mode.hop_limit
    assert not (multi and mode.require_thin)  # pass 2 is searched one target at a time
    lower = ctx.lower_bound(*targets)  # admissible, consistent: A* stays exact per target
    found: dict[int, list[Hop]] = {}
    best: dict[_Key, float] = {start: 0.0}
    back: dict[_Key, tuple[_Key, Hop]] = {}
    # (table, automaton state, thin bit) -> settled (hops, hubs mask, arrived on attribution)
    settled: dict[tuple[int, Any, bool], list[tuple[int, int, bool]]] = {}
    counter = 0
    heap: list[tuple[float, int, int, float, _Key]] = [
        (lower[start[0]], start[2], counter, 0.0, start)
    ]
    costs, attrs = ctx.tables_by_hop()
    while heap:
        _, _, _, cost, key = heapq.heappop(heap)
        table, state, hops, attr, mask, used = key
        here = settled.get((table, state, used))
        if here is None:
            settled[(table, state, used)] = [(hops, mask, attr)]
        elif any(h <= hops and not (m & ~mask) and (attr or not at) for h, m, at in here):
            continue  # dominated (or a duplicate)
        else:
            here.append((hops, mask, attr))
        # Not yet accepting at the destination: the rules may still route through it and back.
        if table in targets and (a is None or a.accepting(state)):
            if not (need_thin and not used) and table not in found:
                out: list[Hop] = []
                k = key
                while k in back:
                    k, hop = back[k]
                    out.append(hop)
                found[table] = out[::-1]
                if len(found) == len(targets):
                    return found
            if not multi:
                continue  # a route ends at its destination (pass 2: without a thin hop, dead)
            # multi-target (pass 1 only): another target's route may run on through this one
        if hops >= limit:
            continue
        leaving_hub = mode.hub_guard and attr and table in hub_ids
        arcs = graph.arcs(table)
        if len(costs) < graph.hop_count():
            costs, attrs = ctx.tables_by_hop()
        at_spur = key is start
        for hop, nxt, bit, idx in arcs:
            if at_spur and spur_banned and hop.key in spur_banned:
                continue
            sup = hop.support
            is_thin = sup is not None and sup < thin
            if prune and is_thin:
                continue
            arrive_attr = attrs[idx]
            if arrive_attr is None:
                arrive_attr = attrs[idx] = ctx.attribution(hop.edge)
            if leaving_hub and arrive_attr:
                continue  # cartesian hazard: attribution in, attribution out
            if bit & mask:
                continue  # a hub is entered at most once
            c = costs[idx]
            if c is None:
                c = costs[idx] = ctx.cost(hop)
            if c == math.inf:
                continue
            nc = cost + c
            f = nc + lower[nxt]
            if f > bound or f == math.inf:
                continue  # cannot reach dst, or (Yen) never picked before k routes are found
            nstate = None if a is None else a.step(state, hop)
            nused = used or (need_thin and is_thin)
            if banned_nodes and (nxt, nstate, nused) in banned_nodes:
                continue
            nkey: _Key = (nxt, nstate, hops + 1, arrive_attr, mask | bit, nused)
            if nc < best.get(nkey, math.inf):
                best[nkey] = nc
                back[nkey] = (key, hop)
                counter += 1
                heapq.heappush(heap, (f, hops + 1, counter, nc, nkey))
    return found


def _route_key(hops: Sequence[Hop]) -> tuple[tuple[str, bool], ...]:
    return tuple(h.key for h in hops)


def _yen(
    ctx: _Ctx, src: ObjectRef, dst: ObjectRef, mode: _Mode, k: int
) -> list[tuple[float, list[Hop]]]:
    """Yen's k cheapest routes over the product graph (table, automaton state, thin bit).

    Routes are simple in that product graph: a spur never re-enters a root node (same table
    *and* same automaton state), and the deviation hops are banned only at the spur node.
    """
    first = _dijkstra(ctx, _replay(ctx, src, (), mode)[0], dst, mode)
    if first is None or k < 1:
        return []
    found: list[tuple[float, list[Hop]]] = [(ctx.total(first), first)]
    seen = {_route_key(first)}
    # route key -> (cost, hops, deviation index)
    pool: dict[tuple[tuple[str, bool], ...], tuple[float, list[Hop], int]] = {}
    deviation = 0
    while len(found) < k:
        prev = found[-1][1]
        need = k - len(found)
        keys = _replay(ctx, src, prev, mode)
        # Lawler: spurs before the previous route's deviation index were already generated.
        for i in range(deviation, len(prev)):
            root = prev[:i]
            rkey = _route_key(root)
            spur_banned = frozenset(
                p[i].key for _, p in found if len(p) > i and _route_key(p[:i]) == rkey
            )
            banned_nodes = frozenset(_node(kk) for kk in keys[: i + 1])  # root incl. spur
            pooled = sorted(c for c, _, _ in pool.values())
            limit = pooled[need - 1] if len(pooled) >= need else math.inf
            root_cost = ctx.total(root)
            tail = _dijkstra(
                ctx,
                keys[i],
                dst,
                mode,
                spur_banned=spur_banned,
                banned_nodes=banned_nodes,
                bound=limit - root_cost + 1e-9,
            )
            if tail is None:
                continue
            route = [*root, *tail]
            key = _route_key(route)
            if key not in seen and key not in pool:
                pool[key] = (ctx.total(route), route, i)
        if not pool:
            break
        pick = min(pool, key=lambda r: (pool[r][0], len(r), repr(r)))
        cost, route, deviation = pool.pop(pick)
        found.append((cost, route))
        seen.add(pick)
    return found


def _path(cost: float, hops: Sequence[Hop], provisional: bool) -> Path:
    supports = [h.support for h in hops]
    coverage = (
        None if any(s is None for s in supports) else min(s for s in supports if s is not None)
    )
    estimated = any(h.edge.estimated for h in hops)
    tables = [hops[0].start, *(h.end for h in hops)] if hops else []
    revisits = len(set(tables)) < len(tables)
    return Path(tuple(hops), cost, coverage, provisional, estimated, revisits)


def _name(t: ObjectRef) -> str:
    return t.canonical()


def _hop_distance(ctx: _Ctx, src: ObjectRef, dst: ObjectRef) -> int | None:
    """Fewest traversable hops ``src`` -> ``dst`` ignoring rules, hub guard and the hop limit."""
    seen = {src}
    frontier = [src]
    depth = 0
    while frontier:
        depth += 1
        nxt: list[ObjectRef] = []
        for t in frontier:
            for hop in ctx.graph.neighbours(t):
                if hop.end in seen or ctx.cost(hop) == math.inf:
                    continue
                if hop.end == dst:
                    return depth
                seen.add(hop.end)
                nxt.append(hop.end)
        frontier = nxt
    return None


def _diagnose(ctx: _Ctx, src: ObjectRef, dst: ObjectRef, automaton: Automaton | None) -> PathResult:
    """Relax exactly one mechanism at a time; the relaxed route only names the blocker."""
    cfg, graph = ctx.cfg, ctx.graph
    pair = f"{_name(src)} -> {_name(dst)}"

    def probe(mode: _Mode) -> list[Hop] | None:
        return _dijkstra(ctx, _replay(ctx, src, (), mode)[0], dst, mode)

    def ruled_needs() -> str:
        """The rules plus a longer hop limit: name both when only that opens a route."""
        # searched without a cap of our own: a simple route cannot use more hops than tables
        longer = probe(_Mode(False, True, max(cfg.hop_limit, len(graph.tables)), automaton))
        if longer is None:
            return ""
        return f"; the required route needs {len(longer)} hops; hop limit is {cfg.hop_limit}"

    if automaton is not None and probe(_Mode(False, True, cfg.hop_limit, None)) is not None:
        return PathResult(
            (),
            Refusal.RULE_REJECTS_ALL,
            f"waypoint rules reject every route {pair}; without them a route exists"
            + ruled_needs(),
        )
    relaxed = probe(_Mode(False, False, cfg.hop_limit, automaton))
    if relaxed is not None:
        pivots = sorted(
            {
                _name(h.end)
                for h, nxt in pairwise(relaxed)
                if graph.hubs.is_hub(h.end)
                and ctx.attribution(h.edge)
                and ctx.attribution(nxt.edge)
            }
        )
        where = (
            f"pivots through hub {', '.join(pivots)} on two attribution edges"
            if pivots
            else "is blocked by the hub guard (an attribution edge into a hub and another out)"
        )
        return PathResult(
            (),
            Refusal.HUB_PIVOT,
            f"every route {pair} {where} (fan-in > {cfg.attribution_fanin:g}): a cartesian hazard",
        )
    need = _hop_distance(ctx, src, dst)
    if need is not None and need > cfg.hop_limit:
        return PathResult(
            (),
            Refusal.NO_ROUTE,
            f"no route {pair} within the hop limit {cfg.hop_limit}; the shortest needs {need} hops",
        )
    if automaton is not None:
        both = ruled_needs()
        if both:
            return PathResult(
                (),
                Refusal.RULE_REJECTS_ALL,
                f"waypoint rules and the hop limit together reject every route {pair}{both}",
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
    diagnose: bool = True,
) -> PathResult:
    """Cheapest routes ``src`` -> ``dst`` (tables; columns are widened).

    Up to ``cfg.k_paths`` well-supported routes (pass 1), then up to ``cfg.k_paths`` routes that
    each take a thin hop (pass 2), labelled ``provisional`` (spec 9.3).
    """
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
        why_fn = getattr(automaton, "why_empty", None)  # optional: name the blocking rule
        why = why_fn() if callable(why_fn) else None
        return PathResult(
            (),
            Refusal.WAYPOINT_UNSATISFIABLE,
            "the waypoint rules for this pair can never be satisfied" + (f": {why}" if why else ""),
        )
    if graph.component(s) != graph.component(d):
        return PathResult(
            (),
            Refusal.NO_ROUTE,
            f"no chain of usable joins connects {_name(s)} and {_name(d)}",
        )
    ctx = _Ctx.of(graph, cfg)
    thin = cfg.thin_support
    firm = _yen(ctx, s, d, _Mode(True, True, cfg.hop_limit, automaton), cfg.k_paths)
    paths = [_path(c, hops, provisional=False) for c, hops in firm]
    if graph.has_thin(s, thin):
        # pass 2: up to k routes that each take a thin hop (pass 1 never does: no duplicates)
        loose = _yen(
            ctx, s, d, _Mode(False, True, cfg.hop_limit, automaton, require_thin=True), cfg.k_paths
        )
        paths += [_path(c, hops, provisional=True) for c, hops in loose]
    if not paths:
        if not diagnose:
            return PathResult(
                (),
                Refusal.NO_ROUTE,
                f"no route {_name(s)} -> {_name(d)} (not diagnosed; diagnose=True names why)",
            )
        return _diagnose(ctx, s, d, automaton)
    provisional = [p for p in paths if p.provisional]
    note = ""
    if provisional:
        narrowest = min((p.coverage for p in provisional if p.coverage is not None), default=None)
        shown = "unknown" if narrowest is None else f"{narrowest:.4g}"
        lead = "every route has" if not firm else f"{len(provisional)} provisional route(s) use"
        note = f"provisional: {lead} a hop with support below {thin:g}; narrowest support {shown}"
    return PathResult(tuple(paths), None, note)


def best_paths_from(
    graph: Graph,
    src: ObjectRef,
    targets: Collection[ObjectRef],
    *,
    cfg: GraphConfig,
    automaton: Automaton | None = None,
) -> dict[ObjectRef, Path | None]:
    """Each target's first (cheapest) route from ``src``: ``find_paths(k_paths=1)`` per target.

    One A* search settles every target (the heuristic is the relaxed distance to the nearest
    target, 0 at each, so each target's first settlement is its optimum). Same rules as
    :func:`find_paths`: thin pass 1, then pass 2 (thin hop required, ``provisional``) only for
    targets still unreached; hub guard; hop limit. No Yen, no diagnosis: an unreachable target
    (or ``src`` itself, a table without edges, another component) maps to ``None``. Columns are
    widened to their tables.
    """
    s = src.table_ref()
    out: dict[ObjectRef, Path | None] = {}
    wanted: dict[int, list[ObjectRef]] = {}
    sid = graph.table_id(s)
    unsat = automaton is not None and automaton.is_empty()
    for target in targets:
        t = target.table_ref()
        out[target] = None
        tid = graph.table_id(t)
        if sid is None or tid is None or t == s or unsat:
            continue
        if graph.component(t) != graph.component(s):
            continue
        wanted.setdefault(tid, []).append(target)
    if not wanted:
        return out
    ctx = _Ctx.of(graph, cfg)
    firm_mode = _Mode(True, True, cfg.hop_limit, automaton)
    start = _replay(ctx, s, (), firm_mode)[0]
    found = _search(ctx, start, frozenset(wanted), firm_mode, multi=True)
    for tid, hops in found.items():
        for target in wanted[tid]:
            out[target] = _path(ctx.total(hops), hops, provisional=False)
    if graph.has_thin(s, cfg.thin_support):
        loose_mode = _Mode(False, True, cfg.hop_limit, automaton, require_thin=True)
        for tid in sorted(set(wanted) - set(found)):
            hops2 = _search(ctx, start, frozenset({tid}), loose_mode, multi=False).get(tid)
            if hops2 is not None:
                for target in wanted[tid]:
                    out[target] = _path(ctx.total(hops2), hops2, provisional=True)
    return out
