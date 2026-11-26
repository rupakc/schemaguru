"""Path search work is bounded by the hop limit, not by the component (0.2.0 spec 6.1).

At scale one join component holds most of the catalog. A search with ``hop_limit`` L can only
finish a route through tables at most L traversable hops from the destination, so the A* bound
is computed for those tables only and every other table is pruned; the result must be exactly
what the unbounded search returns.
"""

from __future__ import annotations

import random
from typing import Any

import pytest

from schemaguru.config import GraphConfig
from schemaguru.graph.edges import Edge
from schemaguru.graph.hubs import Hubs
from schemaguru.graph.search import Graph, Refusal, _Ctx, best_paths_from, find_paths
from schemaguru.model.joins import JoinRef
from schemaguru.model.refs import ObjectRef

CFG = GraphConfig()


def T(name: str) -> ObjectRef:
    return ObjectRef(None, "shop", name)


def _edge(a: str, b: str, i: int, rate: float = 0.97) -> Edge:
    jr = JoinRef((ObjectRef(None, "shop", a, f"k{i}"),), (ObjectRef(None, "shop", b, "id"),))
    return Edge(
        jr, {"match_rate": rate, "coverage": rate, "distinct_left": 100, "fan_in": 1.0}, "OK"
    )


def _far_pair_graph(blob: int = 400) -> Graph:
    """``t0`` .. ``t9`` in a chain (9 hops, over the limit of 6) plus a blob hanging off ``t0``."""
    edges = [_edge(f"t{i}", f"t{i + 1}", 0) for i in range(9)]
    edges += [_edge("t0", f"b{i}", i + 1) for i in range(blob)]
    edges += [_edge(f"b{i}", f"b{i + 1}", 0) for i in range(blob - 1)]
    return Graph.build(edges, Hubs.from_edges(edges, set(), CFG))


def _count_expansions(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls = [0]
    real = Graph.arcs

    def arcs(self: Graph, tid: int) -> Any:
        calls[0] += 1
        return real(self, tid)

    monkeypatch.setattr(Graph, "arcs", arcs)
    return calls


def test_a_pair_beyond_the_hop_limit_is_refused_without_walking_the_component(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    g = _far_pair_graph()
    calls = _count_expansions(monkeypatch)
    res = find_paths(g, T("t0"), T("t9"), cfg=CFG, diagnose=False)
    assert res.refusal is Refusal.NO_ROUTE and not res.paths
    # the blob (400 tables, all within 6 hops of t0) is never expanded: none can reach t9
    assert calls[0] < 20, calls[0]


def test_hazard_scan_does_not_measure_a_plain_no_route(monkeypatch: pytest.MonkeyPatch) -> None:
    """``no_route_detail=False`` (the view's hazard scan) skips the fewest-hops walk that only
    feeds a NO_ROUTE explanation; the refusal kind is unchanged."""
    g = _far_pair_graph()
    import schemaguru.graph.search as search

    detailed = find_paths(g, T("t0"), T("t9"), cfg=CFG)
    assert detailed.refusal is Refusal.NO_ROUTE and "needs 9 hops" in detailed.explanation

    def boom(*_: Any) -> int:
        raise AssertionError("the hop distance was measured")

    monkeypatch.setattr(search, "_hop_distance", boom)
    res = find_paths(g, T("t0"), T("t9"), cfg=CFG, no_route_detail=False)
    assert res.refusal is Refusal.NO_ROUTE and not res.paths


def _random_graph(n: int, m: int, seed: int) -> tuple[Graph, list[str]]:
    rng = random.Random(seed)
    names = [f"n{i}" for i in range(n)]
    edges, seen = [], set()
    while len(edges) < m:
        a, b = rng.sample(names, 2)
        if (a, b) not in seen:
            seen.add((a, b))
            jr = JoinRef((ObjectRef(None, "shop", a, "k"),), (ObjectRef(None, "shop", b, "id"),))
            value = {
                "match_rate": rng.choice([0.3, 0.6, 0.9, 0.99, 1.0]),
                "coverage": rng.choice([0.1, 0.5, 1.0]),
                "distinct_left": rng.choice([None, 10, 1000]),
                "fan_in": rng.choice([1.0, 5.0, 40.0]),
            }
            edges.append(Edge(jr, value, "OK"))
    return Graph.build(edges, Hubs.from_edges(edges, set(), CFG)), names


@pytest.mark.parametrize("seed", [1, 2])
@pytest.mark.parametrize("hop_limit", [1, 2, 4, 6])
@pytest.mark.parametrize("rule", [False, True], ids=["no-rule", "waypoint"])
def test_bounded_search_returns_exactly_the_unbounded_result(
    monkeypatch: pytest.MonkeyPatch, seed: int, hop_limit: int, rule: bool
) -> None:
    """Every route, cost, refusal and explanation equals the search with the bound computed
    over the whole component (ties included: the pruned states can never finish a route),
    for several hop limits, with and without a waypoint rule (whose route may pass through
    the destination and come back)."""
    from tests.unit.graph.test_search_fix2 import Via

    cfg = GraphConfig(hop_limit=hop_limit, k_paths=3)
    rng = random.Random(seed)
    g, names = _random_graph(400, 900, seed)
    pairs = [tuple(rng.sample(names, 2)) for _ in range(40)]
    auto = Via(T(names[7])) if rule else None

    def run(graph: Graph) -> list[Any]:
        out: list[Any] = []
        for a, b in pairs:
            r = find_paths(graph, T(a), T(b), cfg=cfg, automaton=auto)
            out.append(
                (r.refusal, r.explanation, [(p.hops, p.cost, p.provisional) for p in r.paths])
            )
        for a in names[:15]:
            best = best_paths_from(
                graph, T(a), [T(b) for b in names[20:50]], cfg=cfg, automaton=auto
            )
            out.append(sorted((k.canonical(), v) for k, v in best.items()))
        return out

    bounded = run(g)
    real = _Ctx.lower_bound

    def unbounded(self: _Ctx, *dsts: int, radius: int | None = None) -> list[float]:
        return real(self, *dsts)

    monkeypatch.setattr(_Ctx, "lower_bound", unbounded)
    g2, _ = _random_graph(400, 900, seed)  # a fresh graph: no memo shared with the first run
    assert run(g2) == bounded
