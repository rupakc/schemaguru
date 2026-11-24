from __future__ import annotations

import math
from dataclasses import replace

import pytest

from schemaguru.config import GraphConfig
from schemaguru.graph.cost import hop_cost
from schemaguru.graph.edges import Edge, build_edges
from tests.unit.graph.shop_graph import SHOP_JOINS, effective, join_value, measured


def _edge(name: str, **over: object) -> Edge:
    jr, jf = SHOP_JOINS[name]
    (e,) = build_edges(effective([measured(jr, join_value(jf, **over))]))
    return e


def test_support_zero_infinite() -> None:
    # A reverse hop whose coverage is exactly 0 can never be traversed.
    e = _edge("payments->orders", coverage=0.0)
    assert hop_cost(e, reverse=True, in_degree=0, cfg=GraphConfig()) == math.inf
    assert math.isfinite(hop_cost(e, reverse=False, in_degree=0, cfg=GraphConfig()))


def test_formula_matches_spec() -> None:
    e = _edge("orders->customers")  # match 1000/1020, distinct_left 1020
    cfg = GraphConfig()
    got = hop_cost(e, reverse=False, in_degree=1, cfg=cfg)
    want = 1.0 * -math.log(1000 / 1020) + 0.35 * math.log(1 + 1020) + 0.25 * math.log(2)
    assert got == pytest.approx(want)


def test_weights_read_at_call_time() -> None:
    e = _edge("orders->customers")
    base = GraphConfig()
    c0 = hop_cost(e, reverse=False, in_degree=3, cfg=base)
    no_fan = hop_cost(e, reverse=False, in_degree=3, cfg=replace(base, w_fanout=0.0))
    assert c0 - no_fan == pytest.approx(0.35 * math.log(1 + 1020))
    heavy_hub = hop_cost(e, reverse=False, in_degree=3, cfg=replace(base, w_hub=1.0))
    assert heavy_hub - c0 == pytest.approx(0.75 * math.log(4))
    twice = hop_cost(e, reverse=False, in_degree=3, cfg=replace(base, w_support=2.0))
    assert twice - c0 == pytest.approx(-math.log(1000 / 1020))


def test_reverse_traversal_uses_coverage() -> None:
    e = _edge("ref_child->ref_parent")  # forward 1.0, coverage 3/1000
    cfg = replace(GraphConfig(), w_fanout=0.0, w_hub=0.0)
    assert hop_cost(e, reverse=False, in_degree=0, cfg=cfg) == pytest.approx(0.0)
    assert hop_cost(e, reverse=True, in_degree=0, cfg=cfg) == pytest.approx(-math.log(0.003))


def test_unmeasured_costs_penalty_instead_of_support() -> None:
    jr, jf = SHOP_JOINS["payments->orders"]
    cfg = GraphConfig(include_unmeasured=True, unmeasured_penalty=2.5, w_fanout=0.0, w_hub=0.0)
    (e,) = build_edges(effective([measured(jr, join_value(jf, match_rate=None))]), cfg=cfg)
    assert not e.measured
    assert hop_cost(e, reverse=False, in_degree=0, cfg=cfg) == pytest.approx(2.5)
