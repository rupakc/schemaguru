"""Review fix round 1: exact state key, Yen completeness, thin routes appended, unknown fan-out
price, and scale (hub grouping, cached keys, memoised hop cost)."""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, replace
from typing import Any

import pytest

from schemaguru.config import GraphConfig
from schemaguru.graph.calibrate import calibrate_hubs
from schemaguru.graph.cost import hop_cost
from schemaguru.graph.edges import Edge, build_edges
from schemaguru.graph.hubs import Hubs
from schemaguru.graph.search import Graph, Hop, Path, find_paths
from schemaguru.model.facts import Fact
from schemaguru.model.joins import JoinRef
from schemaguru.model.refs import ObjectRef
from tests.fixtures.shop import JoinFacts
from tests.unit.graph.shop_graph import effective, j, join_value, measured, t

CFG = GraphConfig()


def _graph(facts: list[Fact], cfg: GraphConfig = CFG) -> Graph:
    edges = build_edges(effective(facts), cfg=cfg)
    return Graph.build(edges, Hubs.from_edges(edges, set(), cfg))


def _syn(left: str, right: str, rate: float = 1.0, coverage: float | None = None) -> Fact:
    matched = round(rate * 100)
    jf = JoinFacts(100, 100, matched, matched, 100)
    over = {} if coverage is None else {"coverage": coverage}
    return measured(j(left, right), join_value(jf, **over))


def _route(p: Path) -> list[str]:
    return [r.table for r in p.tables]


@dataclass
class Via:
    """Accept only routes through ``table``."""

    table: ObjectRef
    start: bool = False

    def step(self, state: object, hop: Hop) -> object:
        return bool(state) or hop.end == self.table

    def accepting(self, state: object) -> bool:
        return bool(state)

    def is_empty(self) -> bool:
        return False


def test_via_rule_finds_the_route_a_cheaper_label_used_to_block() -> None:
    # S=users, Y=orders, W=sessions, X=products, D=payments, Z=customers.
    # Cheap: S-Y, Y-W, W-X, X-Y, Y-D. Pricier: S-Z, Z-W. "via W" must still find S-Z-W-Y-D
    # (it used to come back RULE_REJECTS_ALL: the cheap label settled Y's state first and the
    # visited-set check then refused it, while the state key did not record the visited set).
    facts = [
        _syn("users.y", "orders.id"),
        _syn("orders.w", "sessions.id"),
        _syn("sessions.x", "products.id"),
        _syn("products.y", "orders.id"),
        _syn("orders.d", "payments.id"),
        _syn("users.z", "customers.id", 0.6),
        _syn("customers.w", "sessions.id", 0.6),
    ]
    cfg = replace(CFG, k_paths=10)
    res = find_paths(
        _graph(facts), t("users"), t("payments"), cfg=cfg, automaton=Via(t("sessions"))
    )
    assert res.refusal is None and res.paths
    assert all("sessions" in _route(p) for p in res.paths)
    routes = [_route(p) for p in res.paths]
    assert ["users", "customers", "sessions", "orders", "payments"] in routes
    for p in res.paths:
        assert p.revisits == (len(set(p.tables)) < len(p.tables))
    assert any(p.revisits for p in res.paths)  # forced by the rule: Y twice, as aliases


def _brute(g: Graph, src: ObjectRef, dst: ObjectRef, cfg: GraphConfig) -> list[float]:
    costs: list[float] = []

    def walk(table: ObjectRef, seen: set[ObjectRef], cost: float, hops: int) -> None:
        if table == dst:
            costs.append(cost)
            return
        if hops == cfg.hop_limit:
            return
        for hop in g.neighbours(table):
            if hop.end in seen:
                continue
            c = hop_cost(
                hop.edge,
                reverse=hop.reverse,
                in_degree=g.in_degree(hop.end),
                cfg=cfg,
                worst_fanout=g.worst_fanout,
            )
            walk(hop.end, seen | {hop.end}, cost + c, hops + 1)

    walk(src, {src}, 0.0, 0)
    return sorted(costs)


@pytest.mark.parametrize("seed", range(12))
def test_yen_is_complete_against_brute_force(seed: int) -> None:
    rng = random.Random(seed)
    names = ["users", "orders", "sessions", "products", "payments", "customers", "events"]
    facts = []
    for i in range(12):
        a, b = rng.sample(names, 2)
        facts.append(_syn(f"{a}.k{i}", f"{b}.id", rng.choice([0.5, 0.7, 0.9, 1.0])))
    cfg = replace(CFG, k_paths=5, hop_limit=4)
    g = _graph(facts, cfg)
    src, dst = t("users"), t("payments")
    if src not in g.tables or dst not in g.tables or g.component(src) != g.component(dst):
        return
    want = _brute(g, src, dst, cfg)[:5]
    got = find_paths(g, src, dst, cfg=cfg).paths
    assert [p.cost for p in got] == pytest.approx(want)
    assert not any(p.revisits for p in got)  # without rules a cheapest route never revisits


def test_thin_direct_join_appended_after_firm_route() -> None:
    facts = [
        _syn("payments.order_id", "orders.order_id", 1.0, coverage=0.03),
        _syn("orders.session_id", "sessions.session_id"),
        _syn("payments.session_id", "sessions.session_id"),
    ]
    res = find_paths(_graph(facts), t("orders"), t("payments"), cfg=CFG)
    assert res.refusal is None
    assert [_route(p) for p in res.paths] == [
        ["orders", "sessions", "payments"],
        ["orders", "payments"],
    ]
    firm, thin = res.paths
    assert not firm.provisional and thin.provisional
    assert thin.coverage == pytest.approx(0.03)
    assert "provisional" in res.explanation


def test_unknown_fanout_never_cheaper_than_known() -> None:
    cfg = replace(GraphConfig(), include_unmeasured=True)
    jf = JoinFacts(5000, 1000, 1000, 5000, 1000)
    facts = [
        measured(j("orders.customer_id", "customers.id"), join_value(jf)),  # OK, dl=1000
        measured(j("orders.account_id", "accounts.id"), join_value(jf, match_rate=None)),
    ]
    edges = build_edges(effective(facts), cfg=cfg)
    g = Graph.build(edges, Hubs.from_edges(edges, set(), cfg))
    assert g.worst_fanout == 1000
    known, unknown = sorted(edges, key=lambda e: e.measured, reverse=True)
    assert known.measured and not unknown.measured
    for w in (0.35, 1.0):
        c = replace(cfg, w_fanout=w)
        ck = hop_cost(known, reverse=False, in_degree=1, cfg=c, worst_fanout=g.worst_fanout)
        cu = hop_cost(unknown, reverse=False, in_degree=1, cfg=c, worst_fanout=g.worst_fanout)
        assert cu >= ck
    # nothing known in the graph: falls back to w_fanout * unmeasured_penalty
    bare = hop_cost(unknown, reverse=False, in_degree=0, cfg=cfg, worst_fanout=None)
    assert bare == pytest.approx(cfg.unmeasured_penalty * (1 + cfg.w_fanout))


# --- scale --------------------------------------------------------------------------------


def _edge(a: str, b: str, rng: random.Random) -> Edge:
    jr = JoinRef((ObjectRef(None, "shop", a, "k"),), (ObjectRef(None, "shop", b, "id"),))
    value = {
        "match_rate": rng.uniform(0.5, 1.0),
        "coverage": rng.uniform(0.06, 1.0),
        "distinct_left": rng.randint(1, 10_000),
        "fan_in": rng.uniform(1.0, 30.0),
    }
    return Edge(jr, value, "OK")


def _sink_edges(n_edges: int, n_sinks: int) -> list[Edge]:
    rng = random.Random(3)
    return [_edge(f"c{i}", f"s{i % n_sinks}", rng) for i in range(n_edges)]


def _random_graph() -> tuple[Graph, list[str]]:
    rng = random.Random(11)
    names = [f"n{i}" for i in range(10_000)]
    edges, seen = [], set()
    while len(edges) < 50_000:
        a, b = rng.sample(names, 2)
        if (a, b) not in seen:
            seen.add((a, b))
            edges.append(_edge(a, b, rng))
    return Graph.build(edges, Hubs.from_edges(edges, set(), CFG)), names


def test_hubs_and_calibration_scale_linearly() -> None:
    def run(n_edges: int, n_sinks: int) -> tuple[float, Hubs, Any]:
        edges = _sink_edges(n_edges, n_sinks)
        t0 = time.perf_counter()
        hubs = Hubs.from_edges(edges, set(), CFG)
        cals = calibrate_hubs(edges)
        return time.perf_counter() - t0, hubs, cals

    small, _, _ = run(5_000, 500)
    large, hubs, cals = run(50_000, 5_000)
    assert len(cals) == 5_000 and hubs.tables
    # a scaling ratio (the absolute bound is the benchmark below): 10x edges and sinks is ~10x
    # the time when linear, ~100x for the old O(sinks x edges); small floored at 10 ms
    assert large / max(small, 0.01) < 30, (small, large)


def test_find_paths_on_a_large_random_graph() -> None:
    g, names = _random_graph()
    res = find_paths(
        g, ObjectRef(None, "shop", names[0]), ObjectRef(None, "shop", names[1]), cfg=CFG
    )
    assert res.paths  # its time is bounded by the benchmark below, never in the default suite


@pytest.mark.benchmark
def test_benchmark_hubs_under_2s() -> None:
    edges = _sink_edges(50_000, 5_000)
    t0 = time.perf_counter()
    Hubs.from_edges(edges, set(), CFG)
    calibrate_hubs(edges)
    assert time.perf_counter() - t0 < 2.0


@pytest.mark.benchmark
def test_benchmark_find_paths_under_1s() -> None:
    g, names = _random_graph()
    rng = random.Random(5)
    for _ in range(5):
        a, b = rng.sample(names, 2)
        t0 = time.perf_counter()
        find_paths(g, ObjectRef(None, "shop", a), ObjectRef(None, "shop", b), cfg=CFG)
        assert time.perf_counter() - t0 < 1.0, (a, b)


def test_edge_and_hop_keys_are_precomputed() -> None:
    e = _edge("orders", "customers", random.Random(1))
    assert e.key == e.join.identity_key() and "key" in vars(e)
    h = Hop(e, True)
    assert h.key == (e.key, True) and h.start == e.right_table and h.end == e.left_table
    assert math.isclose(h.support or 0.0, e.value["coverage"])
