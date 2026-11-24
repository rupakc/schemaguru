from __future__ import annotations

import math

import pytest

from schemaguru.graph.calibrate import calibrate_hubs
from schemaguru.graph.edges import build_edges
from tests.fixtures.shop import JoinFacts
from tests.unit.graph.shop_graph import effective, j, join_value, measured, t


def _fan(left: str, fan_in: float):  # type: ignore[no-untyped-def]
    jf = JoinFacts(int(fan_in * 10), 10, 10, int(fan_in * 10), 100)
    return measured(j(left, "accounts.account_id"), join_value(jf))


def test_calibrate_proposes_gap_centre() -> None:
    facts = [
        _fan("orders.account_id", 1.0),
        _fan("users.account_id", 2.0),
        _fan("events.account_id", 50.0),
        _fan("sessions.account_id", 80.0),
    ]
    (cal,) = calibrate_hubs(build_edges(effective(facts)))
    assert cal.hub == t("accounts")
    assert cal.fan_ins == [1.0, 2.0, 50.0, 80.0]
    assert cal.gap == (2.0, 50.0)  # widest on a log scale: x25 beats x2 and x1.6
    assert cal.proposed_threshold == pytest.approx(math.sqrt(2.0 * 50.0))


def test_calibrate_single_edge_proposes_nothing() -> None:
    (cal,) = calibrate_hubs(build_edges(effective([_fan("orders.account_id", 4.0)])))
    assert cal.fan_ins == [4.0] and cal.gap is None and cal.proposed_threshold is None


def test_calibrate_skips_non_sinks_and_unknown_fan_in() -> None:
    jf = JoinFacts(30, 10, 10, 30, 100)
    facts = [
        _fan("orders.account_id", 3.0),
        measured(j("users.account_id", "accounts.account_id"), join_value(jf, fan_in=None)),
        measured(j("payments.order_id", "orders.order_id"), join_value(jf)),
    ]
    cals = calibrate_hubs(build_edges(effective(facts)))
    assert [c.hub for c in cals] == [t("accounts")]  # orders has an out-edge: not a sink
    assert cals[0].fan_ins == [3.0]
