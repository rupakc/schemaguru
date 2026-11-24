from __future__ import annotations

from schemaguru.config import GraphConfig
from schemaguru.graph.chains import compile_chain
from schemaguru.graph.edges import build_edges
from schemaguru.graph.hubs import Hubs
from schemaguru.graph.search import Graph, find_paths
from schemaguru.graph.waypoints import compile_rules, parse_predicate, validate_rules
from schemaguru.model.chains import Chain, FilterStep, NoteStep
from tests.fixtures.shop import JoinFacts
from tests.unit.graph.shop_graph import effective, j, join_value, measured, t

CFG = GraphConfig()


def _syn(left: str, right: str, rate: float = 1.0):  # type: ignore[no-untyped-def]
    matched = round(rate * 100)
    return measured(j(left, right), join_value(JoinFacts(100, 100, matched, matched, 100)))


FACTS = [
    _syn("events.order_id", "orders.order_id", 1.0),
    _syn("events.session_id", "sessions.session_id", 0.6),
    _syn("orders.session_id", "sessions.session_id", 0.6),
    _syn("payments.order_id", "orders.order_id", 1.0),
]
_E = build_edges(effective(FACTS), cfg=CFG)
G = Graph.build(_E, Hubs.from_edges(_E, set(), CFG))


def test_chain_reoriented_to_graph() -> None:
    # Written orders -> payments; the graph declares payments.order_id -> orders.order_id.
    chain = Chain("order to payment", (j("orders.order_id", "payments.order_id"),), "via order")
    spec = compile_chain(chain, G)
    assert spec is not None
    assert spec.scope == "pair" and spec.tables == (t("orders"), t("payments"))
    assert spec.require == ("edge(shop.payments.order_id->shop.orders.order_id)",)
    assert spec.forbid == () and spec.because == "via order"
    assert spec.source == "chain:order to payment"
    assert parse_predicate(spec.require[0]).join == j("payments.order_id", "orders.order_id")
    assert validate_rules([spec]) == []


def test_multi_step_chain_forces_route() -> None:
    chain = Chain(
        "event to order by session",
        (
            j("sessions.session_id", "events.session_id"),  # reversed vs the graph
            NoteStep("sessions are the bridge"),
            FilterStep(t("sessions"), "kind = 'web'"),
            j("orders.session_id", "sessions.session_id"),
        ),
        "measured: direct match is thin",
    )
    spec = compile_chain(chain, G)
    assert spec is not None
    assert spec.tables == (t("events"), t("orders"))
    assert spec.require == (
        "edge(shop.events.session_id->shop.sessions.session_id)",
        "edge(shop.orders.session_id->shop.sessions.session_id)",
    )
    auto = compile_rules([spec], t("events"), t("orders"))
    (p, *_) = find_paths(G, t("events"), t("orders"), cfg=CFG, automaton=auto).paths
    assert [x.table for x in p.tables] == ["events", "sessions", "orders"]


def test_disconnected_or_nonlinear_chain_is_none() -> None:
    gap = Chain(
        "gap", (j("events.order_id", "orders.order_id"), j("payments.order_id", "nope.id")), "b"
    )
    assert compile_chain(gap, G) is None
    # A step the graph does not declare in either orientation cannot be oriented.
    unknown = Chain("unknown", (j("events.x", "orders.y"),), "b")
    assert compile_chain(unknown, G) is None
    # Revisiting a table is not linear.
    loop = Chain(
        "loop",
        (
            j("events.session_id", "sessions.session_id"),
            j("orders.session_id", "sessions.session_id"),
            j("events.order_id", "orders.order_id"),
        ),
        "b",
    )
    assert compile_chain(loop, G) is None
    assert compile_chain(Chain("empty", (NoteStep("x"),), "b"), G) is None
