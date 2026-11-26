from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from itertools import pairwise

import pytest

from schemaguru.config import GraphConfig
from schemaguru.graph.edges import Edge, build_edges
from schemaguru.graph.hubs import Hubs
from schemaguru.graph.search import Graph, Hop, Path, Refusal, find_paths
from schemaguru.model.facts import Fact
from schemaguru.model.refs import ObjectRef
from tests.fixtures.shop import JoinFacts
from tests.unit.graph.shop_graph import effective, j, join_value, measured, shop_facts, t

CFG = GraphConfig()


def _graph(facts: Sequence[Fact], cfg: GraphConfig = CFG) -> Graph:
    edges = build_edges(effective(facts), cfg=cfg)
    return Graph.build(edges, Hubs.from_edges(edges, set(), cfg))


def _route(p: Path) -> list[str]:
    out = [p.hops[0].start.table]
    out += [h.end.table for h in p.hops]
    return out


def _syn(left: str, right: str, rate: float, *, sampled: bool = False) -> Fact:
    """A synthetic child->parent join: 100 distinct keys, fan-in 1, match ``rate``."""
    matched = round(rate * 100)
    jf = JoinFacts(100, 100, matched, matched, 100)
    return measured(j(left, right), join_value(jf), sampled=sampled)


SHOP = _graph(shop_facts())


def test_reverse_hop_traversed_parent_to_child() -> None:
    res = find_paths(SHOP, t("orders"), t("order_items"), cfg=CFG)
    assert res.refusal is None
    (best, *_) = res.paths
    assert _route(best) == ["orders", "order_items"]
    assert best.hops[0].reverse and best.coverage == pytest.approx(1.0)


def test_thin_join_returned_provisional_with_coverage() -> None:
    res = find_paths(SHOP, t("ref_parent"), t("ref_child"), cfg=CFG)
    assert res.refusal is None
    (p,) = res.paths
    assert p.provisional and p.coverage == pytest.approx(0.003)
    assert "provisional" in res.explanation
    # payments: coverage 0.05 is thin-but-correct, exactly at the threshold -> not provisional
    pay = find_paths(SHOP, t("orders"), t("payments"), cfg=CFG).paths[0]
    assert not pay.provisional and pay.coverage == pytest.approx(0.05)


def test_hub_pivot_refused_accounts() -> None:
    res = find_paths(SHOP, t("orders"), t("events"), cfg=CFG)
    assert res.paths == () and res.refusal is Refusal.HUB_PIVOT
    assert "accounts" in res.explanation
    # the hub as an endpoint is allowed, either way round
    assert _route(find_paths(SHOP, t("orders"), t("accounts"), cfg=CFG).paths[0]) == [
        "orders",
        "accounts",
    ]
    assert _route(find_paths(SHOP, t("accounts"), t("events"), cfg=CFG).paths[0]) == [
        "accounts",
        "events",
    ]


def test_hub_guard_is_a_shape_not_a_node() -> None:
    # Leaving accounts on a non-attribution edge is fine: users.account_id has fan-in 1.
    g = _graph([*shop_facts(), _syn("users.account_id", "accounts.account_id", 1.0)])
    p = find_paths(g, t("orders"), t("users"), cfg=CFG).paths[0]
    assert _route(p) == ["orders", "accounts", "users"]


def test_same_table_refused() -> None:
    res = find_paths(SHOP, t("orders"), ObjectRef(None, "shop", "orders", "order_id"), cfg=CFG)
    assert res.paths == () and res.refusal is Refusal.SAME_TABLE


def test_no_evidence_and_no_route() -> None:
    res = find_paths(SHOP, t("orders"), t("users"), cfg=CFG)
    assert res.refusal is Refusal.NO_EVIDENCE and "users" in res.explanation
    res = find_paths(SHOP, t("orders"), t("ref_parent"), cfg=CFG)
    assert res.refusal is Refusal.NO_ROUTE and res.paths == ()


def test_hop_limit_reported_as_the_blocking_mechanism() -> None:
    res = find_paths(SHOP, t("payments"), t("products"), cfg=replace(CFG, hop_limit=2))
    assert res.refusal is Refusal.NO_ROUTE and res.paths == ()
    assert "hop limit 2" in res.explanation and "3 hops" in res.explanation


def test_hop_in_state_key() -> None:
    # users -> products -> payments -> orders is cheap (3 hops, match 1.0 each);
    # users -> orders is dear (match 0.2). From orders one more hop reaches sessions.
    facts = [
        _syn("users.p", "products.id", 1.0),
        _syn("products.q", "payments.id", 1.0),
        _syn("payments.r", "orders.id", 1.0),
        _syn("users.o", "orders.id", 0.2),
        _syn("orders.s", "sessions.id", 1.0),
    ]
    g = _graph(facts)
    cfg = replace(CFG, hop_limit=3, w_fanout=0.0, w_hub=0.0)
    res = find_paths(g, t("users"), t("sessions"), cfg=cfg)
    assert res.refusal is None
    assert _route(res.paths[0]) == ["users", "orders", "sessions"]
    # with room for it, the cheap 4-hop route wins
    long = find_paths(g, t("users"), t("sessions"), cfg=replace(cfg, hop_limit=4)).paths[0]
    assert len(long.hops) == 4


def test_k_paths_ordered() -> None:
    facts = [
        _syn("users.a", "orders.id", 0.6),
        _syn("users.b", "sessions.id", 1.0),
        _syn("sessions.c", "orders.id", 1.0),
        _syn("users.d", "products.id", 0.9),
        _syn("products.e", "orders.id", 0.9),
    ]
    g = _graph(facts)
    cfg = replace(CFG, w_fanout=0.0, w_hub=0.0)  # costs: sessions 0, products 0.21, direct 0.51
    res = find_paths(g, t("users"), t("orders"), cfg=cfg)
    assert len(res.paths) == 3
    costs = [p.cost for p in res.paths]
    assert costs == sorted(costs)
    routes = [tuple(_route(p)) for p in res.paths]
    assert len(set(routes)) == 3
    assert routes == [
        ("users", "sessions", "orders"),
        ("users", "products", "orders"),
        ("users", "orders"),
    ]
    assert len(find_paths(g, t("users"), t("orders"), cfg=replace(cfg, k_paths=1)).paths) == 1
    # deterministic
    again = find_paths(g, t("users"), t("orders"), cfg=cfg)
    assert [p.cost for p in again.paths] == costs


def test_k_paths_never_loop_through_a_hub_twice_nor_repeat() -> None:
    res = find_paths(SHOP, t("customers"), t("products"), cfg=CFG)
    assert res.refusal is None
    for p in res.paths:
        tables = _route(p)
        assert len(tables) == len(set(tables))


def test_estimated_hop_marks_path() -> None:
    g = _graph([_syn("users.a", "orders.id", 1.0, sampled=True)])
    (p,) = find_paths(g, t("users"), t("orders"), cfg=CFG).paths
    assert p.estimated


def test_unmeasured_hop_has_unknown_coverage() -> None:
    cfg = replace(CFG, include_unmeasured=True)
    jf = JoinFacts(10, 10, 10, 10, 10)
    facts = [
        measured(j("users.a", "orders.id"), join_value(jf, match_rate=None)),
        _syn("orders.s", "sessions.id", 1.0),
    ]
    (p,) = find_paths(_graph(facts, cfg), t("users"), t("sessions"), cfg=cfg).paths
    assert p.coverage is None


# --- waypoint automaton (the protocol Task 10 implements) -----------------------------------


@dataclass
class RequireTable:
    """Accept only paths that pass through ``table`` (state: seen or not)."""

    table: ObjectRef
    empty: bool = False
    start: bool = False

    def step(self, state: object, hop: Hop) -> object:
        return bool(state) or hop.end == self.table

    def accepting(self, state: object) -> bool:
        return bool(state)

    def is_empty(self) -> bool:
        return self.empty


def test_automaton_forces_route() -> None:
    facts = [
        _syn("users.a", "orders.id", 1.0),
        _syn("users.b", "sessions.id", 0.5),
        _syn("sessions.c", "orders.id", 0.5),
    ]
    g = _graph(facts)
    free = find_paths(g, t("users"), t("orders"), cfg=CFG).paths[0]
    assert _route(free) == ["users", "orders"]
    res = find_paths(g, t("users"), t("orders"), cfg=CFG, automaton=RequireTable(t("sessions")))
    assert _route(res.paths[0]) == ["users", "sessions", "orders"]
    assert [_route(p) for p in res.paths if not p.revisits] == [["users", "sessions", "orders"]]


def test_rule_rejects_all_and_unsatisfiable() -> None:
    g = _graph([_syn("users.a", "orders.id", 1.0)])
    res = find_paths(g, t("users"), t("orders"), cfg=CFG, automaton=RequireTable(t("sessions")))
    assert res.refusal is Refusal.RULE_REJECTS_ALL and res.paths == ()
    res = find_paths(
        g, t("users"), t("orders"), cfg=CFG, automaton=RequireTable(t("sessions"), empty=True)
    )
    assert res.refusal is Refusal.WAYPOINT_UNSATISFIABLE


def test_component_adjacency_is_lazy() -> None:
    g = _graph(shop_facts())
    assert g.built_components() == 0
    find_paths(g, t("orders"), t("customers"), cfg=CFG)
    assert g.built_components() == 1
    find_paths(g, t("ref_parent"), t("ref_child"), cfg=CFG)
    assert g.built_components() == 2


def test_edges_property_kept_on_hop() -> None:
    (p,) = find_paths(SHOP, t("payments"), t("orders"), cfg=replace(CFG, k_paths=1)).paths
    assert isinstance(p.hops[0].edge, Edge) and p.hops[0].start == t("payments")


def _many_component_graph(n: int = 12) -> Graph:
    facts = [_syn(f"c{i}a.k", f"c{i}b.id", 1.0) for i in range(n)]
    facts += [_syn(f"c{i}b.k", f"c{i}c.id", 0.9) for i in range(n)]
    return _graph(facts)


def _signature(res: object) -> tuple[object, ...]:
    from schemaguru.graph.search import PathResult

    assert isinstance(res, PathResult)
    return (res.refusal, tuple((_route(p), p.cost) for p in res.paths))


def _hammer(
    shared: Graph, pairs: list[tuple[ObjectRef, ObjectRef]]
) -> tuple[list[BaseException], list[list[tuple[object, ...]]]]:
    """8 threads x 50 ``find_path`` calls on one graph, released together."""
    import threading

    errors: list[BaseException] = []
    results: list[list[tuple[object, ...]]] = []
    barrier = threading.Barrier(8)

    def work(offset: int) -> None:
        try:
            barrier.wait()
            out = []
            for k in range(50):
                a, b = pairs[(offset + k) % len(pairs)]
                out.append(_signature(find_paths(shared, a, b, cfg=CFG)))
            results.append(out)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    return errors, results


def test_shared_graph_thread_safe() -> None:
    import sys

    n = 12
    pairs = [(t(f"c{i}a"), t(f"c{i}c")) for i in range(n)]
    serial_graph = _many_component_graph(n)
    expected = [_signature(find_paths(serial_graph, a, b, cfg=CFG)) for a, b in pairs]
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        for _ in range(5):  # a fresh graph per round, so the lazy builds race every time
            shared = _many_component_graph(n)
            errors, results = _hammer(shared, pairs)
            assert not errors
            assert len(results) == 8 and all(len(out) == 50 for out in results)
            assert all(sig in expected for out in results for sig in out)
            # and the memo agrees with a serial run on the same graph afterwards
            assert [_signature(find_paths(shared, a, b, cfg=CFG)) for a, b in pairs] == expected
    finally:
        sys.setswitchinterval(old)


def test_refusal_names_hop_limit() -> None:
    # users -> orders directly; a rule requiring `far` forces an 8-hop detour, past 2 x the limit
    chain = [f"n{i}" for i in range(9)]
    facts = [_syn(f"{a}.k", f"{b}.id", 1.0) for a, b in pairwise(chain)]
    facts.append(_syn("n0.x", "n8.id", 0.5))
    g = _graph(facts)
    cfg = replace(CFG, hop_limit=2)
    res = find_paths(g, t("n0"), t("n8"), cfg=cfg, automaton=RequireTable(t("n4")))
    assert res.refusal is Refusal.RULE_REJECTS_ALL
    assert "hop limit is 2" in res.explanation
    assert "needs 5 hops" in res.explanation or "needs 8 hops" in res.explanation


def test_via_success_bounded_work(monkeypatch: pytest.MonkeyPatch) -> None:
    import heapq

    from schemaguru.graph import search as search_mod
    from tests.unit.graph.test_search_fix2 import Via, _bench_pairs, _big

    g, names = _big()
    a, b, w = _bench_pairs(names)[0]
    pops = 0
    real = heapq.heappop

    def counting(heap: list[object]) -> object:
        nonlocal pops
        pops += 1
        return real(heap)

    monkeypatch.setattr(search_mod.heapq, "heappop", counting)
    res = find_paths(g, a, b, cfg=GraphConfig(), automaton=Via(w))
    assert res.paths or res.refusal is not None
    print("POPS", pops)
    assert 0 < pops <= 200_000, pops
