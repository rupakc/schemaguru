from __future__ import annotations

from dataclasses import replace

from schemaguru.config import GraphConfig
from schemaguru.graph.edges import build_edges
from schemaguru.graph.hubs import Hubs, is_attribution
from tests.unit.graph.shop_graph import SHOP_JOINS, effective, shop_facts, t


def _edges():  # type: ignore[no-untyped-def]
    return build_edges(effective(shop_facts()))


def test_is_attribution_uses_fan_in_threshold() -> None:
    by = {e.join.identity_key(): e for e in _edges()}
    cfg = GraphConfig()
    assert is_attribution(by[SHOP_JOINS["orders->accounts"][0].identity_key()], cfg)  # 250
    assert is_attribution(by[SHOP_JOINS["events->accounts"][0].identity_key()], cfg)  # 333
    oc = by[SHOP_JOINS["orders->customers"][0].identity_key()]  # 10000/1020 = 9.8
    assert not is_attribution(oc, cfg)
    assert is_attribution(oc, replace(cfg, attribution_fanin=9.0))


def test_hub_measured_shape_without_entity_type() -> None:
    hubs = Hubs.from_edges(_edges(), set(), GraphConfig())
    # accounts: a sink reached by two attribution edges from distinct tables (orders, events).
    assert hubs.tables == frozenset({t("accounts")})


def test_entity_type_makes_a_sink_a_hub() -> None:
    hubs = Hubs.from_edges(_edges(), {t("customers"), t("orders")}, GraphConfig())
    # customers is a sink -> hub; orders has out-edges -> never a hub.
    assert hubs.tables == frozenset({t("accounts"), t("customers")})


def test_one_attribution_edge_is_not_a_hub() -> None:
    edges = build_edges(effective(shop_facts(skip=("events->accounts",))))
    assert Hubs.from_edges(edges, set(), GraphConfig()).tables == frozenset()
