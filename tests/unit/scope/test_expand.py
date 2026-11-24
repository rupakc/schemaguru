from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from schemaguru.config import GraphConfig, ScopeConfig
from schemaguru.graph.edges import build_edges
from schemaguru.graph.hubs import Hubs
from schemaguru.graph.search import Graph
from schemaguru.model.facts import Fact
from schemaguru.scope.expand import expand
from tests.fixtures.shop import JoinFacts
from tests.unit.graph.shop_graph import effective, j, join_value, measured, shop_facts, t

CFG = GraphConfig()


def _graph(facts: Sequence[Fact]) -> Graph:
    edges = build_edges(effective(facts), cfg=CFG)
    return Graph.build(edges, Hubs.from_edges(edges, set(), CFG))


def _syn(left: str, right: str) -> Fact:
    jf = JoinFacts(100, 100, 100, 100, 100)
    return measured(j(left, right), join_value(jf))


def test_expand_adds_bridge_table() -> None:
    # events -> sessions <- orders: sessions bridges the two seeds.
    g = _graph([*shop_facts(), _syn("orders.session_id", "sessions.session_id")])
    assert expand([t("events"), t("orders")], g, ScopeConfig()) == [t("sessions")]
    # seeds already adjacent: nothing to add; a seed is never returned as a bridge
    assert expand([t("orders"), t("customers")], g, ScopeConfig()) == []
    assert expand([t("events"), t("orders"), t("sessions")], g, ScopeConfig()) == []


def test_expand_ranks_by_path_cost_and_caps() -> None:
    g = _graph(shop_facts())
    # payments -> orders -> customers (1 bridge) and payments ... products via order_items
    got = expand([t("payments"), t("customers"), t("products")], g, ScopeConfig())
    assert set(got) == {t("orders"), t("order_items")}
    capped = expand(
        [t("payments"), t("customers"), t("products")],
        g,
        replace(ScopeConfig(), max_expansion_tables=1),
    )
    assert len(capped) == 1 and capped[0] == got[0]
    assert (
        expand([t("payments"), t("products")], g, replace(ScopeConfig(), max_expansion_tables=0))
        == []
    )


def test_expand_tolerates_refusals_and_unknown_seeds() -> None:
    g = _graph(shop_facts())
    # orders <-> events pivots through the accounts hub: refused, so no bridge is invented
    assert expand([t("orders"), t("events")], g, ScopeConfig()) == []
    assert expand([t("nope"), t("orders")], g, ScopeConfig()) == []
    assert expand([], g, ScopeConfig()) == []
    assert expand([t("orders"), t("orders")], g, ScopeConfig()) == []


def test_expand_is_deterministic() -> None:
    g = _graph(shop_facts())
    seeds = [t("payments"), t("customers"), t("products")]
    assert expand(seeds, g, ScopeConfig()) == expand(seeds, g, ScopeConfig())
