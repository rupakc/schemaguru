from __future__ import annotations

import math

from schemaguru.config import GraphConfig
from schemaguru.diagnostics import DiagnosticBag
from schemaguru.graph.edges import build_edges, entity_tables
from schemaguru.knowledge.resolve import Effective
from schemaguru.model.facts import Fact, Provenance, Status
from schemaguru.model.kinds import FactKind
from tests.unit.graph.shop_graph import (
    NOW,
    SHOP_JOINS,
    effective,
    entity,
    join_value,
    measured,
    shop_facts,
    t,
)


def _keys(edges: list) -> set[str]:  # type: ignore[type-arg]
    return {e.join.identity_key() for e in edges}


def test_broken_join_refused() -> None:
    edges = build_edges(effective(shop_facts()))
    keys = _keys(edges)
    broken = SHOP_JOINS["broken_child->ref_parent"][0].identity_key()
    no_direct = SHOP_JOINS["events->orders"][0].identity_key()
    assert broken not in keys and no_direct not in keys
    assert len(edges) == len(SHOP_JOINS) - 2
    verdicts = {e.join.identity_key(): e.verdict for e in edges}
    assert verdicts[SHOP_JOINS["orders->customers"][0].identity_key()] == "OK"  # 1000/1020 = 0.98
    assert verdicts[SHOP_JOINS["payments->orders"][0].identity_key()] == "OK"
    assert all(e.measured and not e.estimated for e in edges)


def test_nan_and_inf_are_unmeasured() -> None:
    jr, jf = SHOP_JOINS["payments->orders"]
    facts = [measured(jr, join_value(jf, match_rate=math.nan))]
    assert build_edges(effective(facts)) == []
    cand = build_edges(effective(facts), cfg=GraphConfig(include_unmeasured=True))
    assert len(cand) == 1 and not cand[0].measured and cand[0].verdict is None
    jr2, jf2 = SHOP_JOINS["ref_child->ref_parent"]
    only_cov = build_edges(effective([measured(jr2, join_value(jf2, coverage=math.inf))]))
    assert len(only_cov) == 1 and only_cov[0].value["coverage"] is None


def test_unmeasured_candidate_only_when_included() -> None:
    jr, _ = SHOP_JOINS["payments->orders"]
    declared = Fact.make(
        FactKind.JOIN, jr, {}, Provenance.DECLARED, Status.UNVERIFIED, source="dbt"
    )
    assert build_edges(effective([declared])) == []
    edges = build_edges(effective([declared]), cfg=GraphConfig(include_unmeasured=True))
    assert [e.measured for e in edges] == [False]


def test_malformed_costs_one_edge_with_sg601() -> None:
    bad_jr, jf = SHOP_JOINS["payments->orders"]
    bad = measured(bad_jr, join_value(jf, match_rate="high"))
    rate_jr, jf2 = SHOP_JOINS["order_items->orders"]
    over = measured(rate_jr, join_value(jf2, coverage=1.7))
    good = shop_facts(skip=("payments->orders", "order_items->orders"))
    bag = DiagnosticBag()
    edges = build_edges(effective([bad, over, *good]), bag=bag)
    assert len(edges) == len(SHOP_JOINS) - 4  # 2 malformed + broken + no-direct
    assert [d.code for d in bag] == ["SG601", "SG601"]
    assert {d.location for d in bag} == {bad_jr.identity_key(), rate_jr.identity_key()}


def test_exact_preferred_over_newer_estimate() -> None:
    jr, jf = SHOP_JOINS["payments->orders"]
    exact = measured(jr, join_value(jf))
    sample = Fact.make(
        FactKind.JOIN,
        jr,
        join_value(jf, match_rate=0.5),
        Provenance.MEASURED,
        Status.ESTIMATED,
        method="sampled:keyhash:0.1",
        observed_at=NOW.replace(hour=13),
    )
    (edge,) = build_edges(effective([exact, sample]))
    assert edge.value["match_rate"] == 1.0 and not edge.estimated
    (only,) = build_edges(effective([sample]))
    assert only.estimated and only.value["match_rate"] == 0.5


def test_error_fact_does_not_hide_good_exact() -> None:
    jr, jf = SHOP_JOINS["payments->orders"]
    good = measured(jr, join_value(jf))
    err = Fact.make(
        FactKind.JOIN,
        jr,
        {"reason": "timeout"},
        Provenance.MEASURED,
        Status.ERROR,
        method="error",
        observed_at=NOW.replace(hour=14),
    )
    # Built directly: an ERROR must never stand in for the last good exact measurement, even
    # when the store keeps both (the ruling that gives ERROR its own method class).
    eff = Effective(FactKind.JOIN, jr, err, None, err, (), (err, good))
    (edge,) = build_edges({("JOIN", jr.identity_key()): eff})
    assert edge.value["match_rate"] == 1.0


def test_entity_tables_from_effective() -> None:
    eff = effective([*shop_facts(), entity(t("customers"))])
    assert entity_tables(eff) == {t("customers")}


def test_graph_codes_registered() -> None:
    from pathlib import Path

    from schemaguru.diagnostics import CODES

    doc = (Path(__file__).resolve().parents[3] / "docs/reference/diagnostics.md").read_text(
        encoding="utf-8"
    )
    assert "SG601" in CODES and "| SG601 |" in doc
