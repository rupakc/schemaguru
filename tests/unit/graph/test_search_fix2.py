"""Review fix round 2: Yen over the product graph, pass 2 requires a thin hop, the destination
may be passed through while the rules are unsatisfied, rule + hop-limit refusals name both,
``find_paths(diagnose=False)`` and ``best_paths_from``."""

from __future__ import annotations

import math
import random
import time
from dataclasses import replace
from typing import Any

import pytest

from schemaguru.config import GraphConfig
from schemaguru.graph.cost import hop_cost
from schemaguru.graph.edges import Edge
from schemaguru.graph.hubs import Hubs, is_attribution
from schemaguru.graph.search import Graph, Hop, Refusal, best_paths_from, find_paths
from schemaguru.graph.waypoints import compile_rules
from schemaguru.model.joins import JoinRef
from schemaguru.model.refs import ObjectRef
from tests.unit.graph.test_search_fix1 import _random_graph
from tests.unit.graph.test_waypoints import CFG as WCFG
from tests.unit.graph.test_waypoints import G as WG
from tests.unit.graph.test_waypoints import pair


def T(name: str) -> ObjectRef:
    return ObjectRef(None, "shop", name)


def E(a: str, b: str, i: int, mr: float, cov: float, dl: int | None, fan: float) -> Edge:
    jr = JoinRef((ObjectRef(None, "shop", a, f"k{i}"),), (ObjectRef(None, "shop", b, "id"),))
    value = {"match_rate": mr, "coverage": cov, "distinct_left": dl, "fan_in": fan}
    return Edge(jr, value, "OK" if mr >= 0.95 else "WEAK")


class Via:
    """``via`` w: accept routes through w; ``neg``: accept only routes that avoid w."""

    def __init__(self, w: ObjectRef, neg: bool = False) -> None:
        self.w, self.neg = w, neg

    @property
    def start(self) -> bool:
        return self.neg

    def step(self, state: Any, hop: Hop) -> bool:
        if self.neg:
            return bool(state) and hop.end != self.w
        return bool(state) or hop.end == self.w

    def accepting(self, state: Any) -> bool:
        return bool(state)

    def is_empty(self) -> bool:
        return False


def brute(
    g: Graph, cfg: GraphConfig, src: ObjectRef, dst: ObjectRef, auto: Via | None, loose: bool
) -> list[float]:
    """Costs of every walk with no repeated (table, automaton state[, thin bit in pass 2])."""
    out: list[float] = []
    thin = cfg.thin_support

    def walk(t: ObjectRef, st: Any, cost: float, hops: int, attr: bool, mask: int,
             used: bool, seen: frozenset[tuple[ObjectRef, Any, bool]]) -> None:  # fmt: skip
        seen = seen | {(t, st, used)}
        if t == dst and (auto is None or auto.accepting(st)):
            if not loose or used:
                out.append(cost)
            return  # a route ends at its destination
        if hops == cfg.hop_limit:
            return
        for h in g.neighbours(t):
            ns = None if auto is None else auto.step(st, h)
            is_thin = h.support is not None and h.support < thin
            nused = used or (loose and is_thin)
            if (h.end, ns, nused) in seen:
                continue
            if not loose and is_thin:
                continue
            a = is_attribution(h.edge, cfg)
            if g.hubs.is_hub(t) and attr and a:
                continue
            bit = g.hub_bit(h.end)
            if bit & mask:
                continue
            c = hop_cost(
                h.edge,
                reverse=h.reverse,
                in_degree=g.in_degree(h.end),
                cfg=cfg,
                worst_fanout=g.worst_fanout,
            )
            if c == math.inf:
                continue
            walk(h.end, ns, cost + c, hops + 1, a, mask | bit, nused, seen)

    walk(src, None if auto is None else auto.start, 0.0, 0, False, g.hub_bit(src), False,
         frozenset())  # fmt: skip
    return sorted(out)


def _random_case(seed: int) -> tuple[Graph, GraphConfig, list[str], random.Random]:
    rng = random.Random(seed)
    n, m = rng.choice([6, 7, 8, 9]), rng.choice([10, 13, 16])
    names = [f"n{i}" for i in range(n)]
    es = []
    for i in range(m):
        a, b = rng.sample(names, 2)
        es.append(
            E(a, b, i, rng.choice([0.5, 0.7, 0.9, 1.0]), rng.choice([0.03, 0.3, 0.8, 1.0]),
              rng.choice([1, 10, 1000, None]), rng.choice([1.0, 2.0, 50.0]))
        )  # fmt: skip
    for hub in ("H1", "H2"):  # sinks with attribution in-edges from distinct tables
        for i, a in enumerate(rng.sample(names, 3)):
            es.append(E(a, hub, 100 + i, rng.choice([0.9, 1.0]), rng.choice([0.5, 1.0]),
                        rng.choice([10, 1000]), 50.0))  # fmt: skip
    cfg = replace(GraphConfig(), k_paths=5, hop_limit=5)
    return Graph.build(es, Hubs.from_edges(es, set(), cfg)), cfg, names, rng


def _cases(seed: int):  # type: ignore[no-untyped-def]
    g, cfg, names, rng = _random_case(seed)
    for _ in range(4):
        src, dst = (T(x) for x in rng.sample([*names, "H1"], 2))
        kind = rng.choice(["none", "via", "notvia"])
        w = T(rng.choice([*names, "H1", "H2"]))
        if src not in g.tables or dst not in g.tables or g.component(src) != g.component(dst):
            continue
        yield g, cfg, src, dst, (None if kind == "none" else Via(w, neg=kind == "notvia"))


@pytest.mark.parametrize("seed", range(50))
def test_yen_matches_brute_force_with_rules_and_hubs(seed: int) -> None:
    for g, cfg, src, dst, auto in _cases(seed):
        res = find_paths(g, src, dst, cfg=cfg, automaton=auto)
        firm = [p.cost for p in res.paths if not p.provisional]
        loose = [p.cost for p in res.paths if p.provisional]
        assert firm == pytest.approx(brute(g, cfg, src, dst, auto, False)[:5])
        want_loose = brute(g, cfg, src, dst, auto, True)[:5]
        if g.has_thin(src, cfg.thin_support):
            assert loose == pytest.approx(want_loose)
        else:
            assert loose == [] and want_loose == []


@pytest.mark.parametrize("seed", range(50))
def test_best_paths_from_equals_first_route(seed: int) -> None:
    for g, cfg, src, _, auto in _cases(seed):
        targets = sorted(g.tables - {src}, key=lambda r: r.table)
        got = best_paths_from(g, src, targets, cfg=cfg, automaton=auto)
        for target in targets:
            one = find_paths(g, src, target, cfg=replace(cfg, k_paths=1), automaton=auto)
            first = one.paths[0] if one.paths else None
            mine = got[target]
            assert (mine is None) == (first is None), target
            if mine is not None and first is not None:
                assert mine.cost == pytest.approx(first.cost)
                assert mine.provisional == first.provisional


def test_best_paths_from_edge_cases() -> None:
    g, cfg, names, _ = _random_case(3)
    src = T(names[0])
    out = best_paths_from(g, src, [src, T("nowhere")], cfg=cfg)
    assert out == {src: None, T("nowhere"): None}


def test_pass_two_returns_thin_routes_even_behind_k_firm_ones() -> None:
    cfg = GraphConfig()  # k = 3
    for firm_routes in (3, 4):
        es = [E("payments", "orders", 0, 0.99, 0.03, 10, 2.0)]
        for i in range(firm_routes):
            es += [E(f"x{i}", "orders", 10 + i, 1.0, 1.0, 10, 2.0)]
            es += [E(f"x{i}", "payments", 20 + i, 1.0, 1.0, 10, 2.0)]
        g = Graph.build(es, Hubs(frozenset()))
        res = find_paths(g, T("orders"), T("payments"), cfg=cfg)
        assert [p.provisional for p in res.paths] == [False, False, False, True]
        thin = res.paths[-1]
        assert [r.table for r in thin.tables] == ["orders", "payments"]
        assert thin.coverage == pytest.approx(0.03)


def test_destination_passed_through_while_rules_unsatisfied() -> None:
    def run(a: str, b: str) -> list[list[str]]:
        auto = compile_rules([pair(a, b, require=["node(shop.payments)"])], T(a), T(b))
        res = find_paths(WG, T(a), T(b), cfg=WCFG, automaton=auto)
        assert res.refusal is None, res.explanation
        return [[r.table for r in p.tables] for p in res.paths]

    there = run("events", "orders")  # payments hangs off orders: through dst and back
    assert there[0] == ["events", "orders", "payments", "orders"]
    back = run("orders", "events")
    assert back[0][-1] == "events" and "payments" in back[0]


def test_rules_and_hop_limit_named_together() -> None:
    es = [
        E("users", "orders", 0, 1.0, 1.0, 10, 1.0),
        E("users", "sessions", 1, 1.0, 1.0, 10, 1.0),
        E("sessions", "products", 2, 1.0, 1.0, 10, 1.0),
        E("products", "orders", 3, 1.0, 1.0, 10, 1.0),
    ]
    g = Graph.build(es, Hubs(frozenset()))
    cfg = replace(GraphConfig(), hop_limit=2)
    res = find_paths(g, T("users"), T("orders"), cfg=cfg, automaton=Via(T("products")))
    assert res.refusal is Refusal.RULE_REJECTS_ALL
    assert "the required route needs 3 hops; hop limit is 2" in res.explanation


def test_no_diagnosis_on_request() -> None:
    from tests.unit.graph.shop_graph import t
    from tests.unit.graph.test_search import SHOP

    res = find_paths(SHOP, t("orders"), t("events"), cfg=GraphConfig(), diagnose=False)
    assert res.refusal is Refusal.NO_ROUTE and "not diagnosed" in res.explanation
    full = find_paths(SHOP, t("orders"), t("events"), cfg=GraphConfig())
    assert full.refusal is Refusal.HUB_PIVOT


# --- scale ----------------------------------------------------------------------------------


def _bench_pairs(names: list[str]) -> list[tuple[ObjectRef, ObjectRef, ObjectRef]]:
    rng = random.Random(9)
    return [tuple(T(x) for x in rng.sample(names, 3)) for _ in range(3)]  # type: ignore[misc]


def _big() -> tuple[Graph, list[str]]:
    g, names = _random_graph()
    g.neighbours(T(names[0]))  # build the (single) component once, outside the timings
    return g, names


def test_via_rule_and_multi_target_on_a_large_graph() -> None:
    g, names = _big()
    a, b, w = _bench_pairs(names)[0]
    res = find_paths(g, a, b, cfg=GraphConfig(), automaton=Via(w))
    targets = [T(x) for x in names[1:8]]
    out = best_paths_from(g, a, targets, cfg=GraphConfig())
    # correctness only: the time bounds are the benchmarks below, never the default suite
    assert res.paths or res.refusal is not None
    assert set(out) == set(targets)


@pytest.mark.benchmark
def test_benchmark_via_rule_under_1s() -> None:
    g, names = _big()
    for a, b, w in _bench_pairs(names):
        for auto in (Via(w), Via(w, neg=True)):
            t0 = time.perf_counter()
            find_paths(g, a, b, cfg=GraphConfig(), automaton=auto)
            assert time.perf_counter() - t0 < 1.0, (a, b, w)


@pytest.mark.benchmark
def test_benchmark_via_rule_refusal_under_1s() -> None:
    g, names = _big()
    a, b, _ = _bench_pairs(names)[0]
    ghost = Via(T("not_in_graph"))  # a rule no route can satisfy: refusal plus diagnosis
    t0 = time.perf_counter()
    res = find_paths(g, a, b, cfg=GraphConfig(), automaton=ghost)
    assert res.refusal is Refusal.RULE_REJECTS_ALL
    assert time.perf_counter() - t0 < 1.0


@pytest.mark.benchmark
def test_benchmark_best_paths_from_under_300ms() -> None:
    g, names = _big()
    targets = [T(x) for x in names[1:8]]
    t0 = time.perf_counter()
    out = best_paths_from(g, T(names[0]), targets, cfg=GraphConfig())
    assert time.perf_counter() - t0 < 0.3
    assert all(p is not None for p in out.values())


def test_bulk_cost_fill_matches_hop_cost() -> None:
    from schemaguru.graph.search import _Ctx

    for seed in range(5):
        g, cfg, names, _ = _random_case(seed)
        for n in names:
            g.neighbours(T(n))
        ctx = _Ctx(g, replace(cfg, w_fanout=0.7, w_hub=0.4, unmeasured_penalty=1.3))
        bulk = ctx._fill_costs()
        for idx, (hop, _) in enumerate(g._hops):
            want = hop_cost(
                hop.edge, reverse=hop.reverse, in_degree=g.in_degree(hop.end), cfg=ctx.cfg,
                worst_fanout=g.worst_fanout,
            )  # fmt: skip
            assert bulk[idx] == pytest.approx(want)
