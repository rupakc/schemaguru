from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from schemaguru.config import GraphConfig
from schemaguru.diagnostics import CODES, DiagnosticBag, Severity
from schemaguru.graph.edges import build_edges
from schemaguru.graph.hubs import Hubs
from schemaguru.graph.search import Graph, Refusal, find_paths
from schemaguru.graph.waypoints import (
    MAX_REQUIREMENTS,
    Predicate,
    compile_rules,
    lint_rules,
    parse_predicate,
    parse_term,
    validate_rules,
)
from schemaguru.model.facts import Fact
from schemaguru.model.refs import ObjectRef
from schemaguru.overlays.schema import WaypointSpec
from tests.fixtures.shop import JoinFacts
from tests.unit.graph.shop_graph import effective, j, join_value, measured, t

CFG = GraphConfig()


def _syn(left: str, right: str, rate: float = 1.0) -> Fact:
    matched = round(rate * 100)
    return measured(j(left, right), join_value(JoinFacts(100, 100, matched, matched, 100)))


def _graph(facts: Sequence[Fact]) -> Graph:
    edges = build_edges(effective(facts), cfg=CFG)
    return Graph.build(edges, Hubs.from_edges(edges, set(), CFG))


def _simple(res_paths: Sequence[object]) -> list[list[str]]:
    """Routes that visit no table twice (revisits are legitimate when a rule forces one)."""
    return _route([p for p in res_paths if not p.revisits])  # type: ignore[attr-defined]


def _route(res_paths: Sequence[object]) -> list[list[str]]:
    out = []
    for p in res_paths:
        tables = p.tables  # type: ignore[attr-defined]
        out.append([x.table for x in tables])
    return out


def pair(
    a: str, b: str, *, require: Sequence[str] = (), forbid: Sequence[str] = (), because: str = "b"
) -> WaypointSpec:
    return WaypointSpec("pair", (t(a), t(b)), tuple(require), tuple(forbid), because, "o.yaml:3")


def node(n: str, *, forbid: Sequence[str] = (), require: Sequence[str] = ()) -> WaypointSpec:
    return WaypointSpec("node", (t(n),), tuple(require), tuple(forbid), "b", "o.yaml:9")


# events -> orders: a cheap direct join, and a dearer route through sessions.
FACTS = [
    _syn("events.order_id", "orders.order_id", 1.0),
    _syn("events.session_id", "sessions.session_id", 0.6),
    _syn("orders.session_id", "sessions.session_id", 0.6),
    _syn("payments.order_id", "orders.order_id", 1.0),
]
G = _graph(FACTS)
VIA = "edge(shop.events.session_id->shop.sessions.session_id)"


# --- parsing -----------------------------------------------------------------------------------


def test_parse_predicates_exact() -> None:
    assert parse_predicate("any") == Predicate("any")
    assert parse_predicate(" node(shop.orders) ") == Predicate("node", table=t("orders"))
    assert parse_predicate("col(shop.orders.status)") == Predicate(
        "col", column=ObjectRef(None, "shop", "orders", "status")
    )
    e = parse_predicate("edge(shop.events.session_id -> shop.sessions.session_id)")
    assert e.kind == "edge" and e.join == j("events.session_id", "sessions.session_id")
    comp = parse_predicate('edge(shop.a.x, shop.a."y.z"->shop.b.x, shop.b.w)')
    assert comp.join is not None and len(comp.join.left) == 2
    assert comp.join.left[1].column == "y.z"


@pytest.mark.parametrize(
    "token",
    [
        "",
        "  ",
        "shop.orders.status.x",  # a bare reference must be a table
        "node()",
        "node(shop.orders",
        "nodes(shop.orders)",
        "col(orders)",  # a column needs a table
        "edge(shop.a.x)",
        "edge(shop.a.x->shop.b.y->shop.c.z)",
        "edge(shop.a.x, shop.b.y->shop.c.z, shop.c.w)",  # one side spans two tables
        "edge(shop.a.x->shop.b.y, shop.b.z)",  # unequal sides
        "Any(x)",
        'node("shop.orders)',
        "shop..orders",
    ],
)
def test_bad_token_fails_closed(token: str) -> None:
    with pytest.raises(ValueError):
        parse_predicate(token)


def test_bare_table_ref_is_node() -> None:
    assert parse_predicate("shop.orders") == Predicate("node", table=t("orders"))
    assert parse_predicate(" ANY ") == Predicate("node", table=ObjectRef(None, None, "ANY"))
    assert parse_term("shop.a | node(shop.b)") == (
        Predicate("node", table=t("a")),
        Predicate("node", table=t("b")),
    )
    with pytest.raises(ValueError):
        parse_term("node(shop.a) |")
    # Bare form in a pair rule forces the route like node() does.
    auto = compile_rules(
        [pair("events", "orders", require=["shop.sessions"])], t("events"), t("orders")
    )
    # the only simple route; others revisit a table, which the rule may force (spec 9.3)
    assert _simple(find_paths(G, t("events"), t("orders"), cfg=CFG, automaton=auto).paths) == [
        ["events", "sessions", "orders"]
    ]


def test_bad_rule_reported_and_refuses_its_searches() -> None:
    bad = pair("events", "orders", require=["sessions("])
    bag = DiagnosticBag()
    auto = compile_rules([bad], t("events"), t("orders"), bag=bag)
    (d,) = list(bag)
    assert d.code == "SG602" and d.severity is Severity.ERROR and d.location == "o.yaml:3"
    assert auto.is_empty()
    res = find_paths(G, t("events"), t("orders"), cfg=CFG, automaton=auto)
    assert res.refusal is Refusal.WAYPOINT_UNSATISFIABLE and "SG602" in res.explanation
    # A broken rule for another pair does not govern this search, but is still reported.
    bag2 = DiagnosticBag()
    other = compile_rules([bad], t("payments"), t("orders"), bag=bag2)
    assert not other.is_empty() and [x.code for x in bag2] == ["SG602"]


def test_node_rule_with_require_or_no_because_is_malformed() -> None:
    codes = [d.code for d in validate_rules([node("sessions", require=["any"])])]
    assert codes == ["SG602"]
    assert [d.code for d in validate_rules([pair("a", "b", require=["any"], because=" ")])] == [
        "SG602"
    ]
    assert validate_rules([pair("a", "b", require=[VIA])]) == []


def test_more_than_16_requirements_rejected() -> None:
    reqs = [f"node(shop.t{i})" for i in range(MAX_REQUIREMENTS + 1)]
    assert MAX_REQUIREMENTS == 16
    (d,) = validate_rules([pair("events", "orders", require=reqs)])
    assert d.code == "SG603" and d.severity is Severity.ERROR and d.location == "o.yaml:3"
    assert compile_rules(
        [pair("events", "orders", require=reqs)], t("events"), t("orders")
    ).is_empty()
    assert validate_rules([pair("events", "orders", require=reqs[:16])]) == []


# --- search integration ------------------------------------------------------------------------


def test_require_edge_forces_route_via_sessions() -> None:
    free = find_paths(G, t("events"), t("orders"), cfg=CFG)
    assert _route(free.paths)[0] == ["events", "orders"]
    auto = compile_rules([pair("events", "orders", require=[VIA])], t("events"), t("orders"))
    res = find_paths(G, t("events"), t("orders"), cfg=CFG, automaton=auto)
    assert res.refusal is None
    assert _simple(res.paths) == [["events", "sessions", "orders"]]
    # A pair rule is a property of the pair, not of the direction searched.
    back = compile_rules([pair("events", "orders", require=[VIA])], t("orders"), t("events"))
    assert _simple(find_paths(G, t("orders"), t("events"), cfg=CFG, automaton=back).paths) == [
        ["orders", "sessions", "events"]
    ]
    # ... and it does not govern any other pair.
    other = compile_rules([pair("events", "orders", require=[VIA])], t("events"), t("payments"))
    assert _route(find_paths(G, t("events"), t("payments"), cfg=CFG, automaton=other).paths)[0] == [
        "events",
        "orders",
        "payments",
    ]


def test_require_col_and_node() -> None:
    for req in ("node(shop.sessions)", "col(shop.orders.session_id)"):
        auto = compile_rules([pair("events", "orders", require=[req])], t("events"), t("orders"))
        res = find_paths(G, t("events"), t("orders"), cfg=CFG, automaton=auto)
        assert _simple(res.paths) == [["events", "sessions", "orders"]], req


def test_forbid_node() -> None:
    rules = [node("sessions", forbid=["node(shop.sessions)"])]
    auto = compile_rules(rules, t("events"), t("payments"))
    res = find_paths(G, t("events"), t("payments"), cfg=CFG, automaton=auto)
    assert all("sessions" not in r for r in _route(res.paths)) and res.paths
    # Forbidding the direct edge leaves only the route through sessions.
    auto = compile_rules(
        [pair("events", "orders", forbid=["edge(shop.events.order_id->shop.orders.order_id)"])],
        t("events"),
        t("orders"),
    )
    res = find_paths(G, t("events"), t("orders"), cfg=CFG, automaton=auto)
    assert _simple(res.paths) == [["events", "sessions", "orders"]]


def test_node_rule_bans_its_node_as_intermediate_only() -> None:
    rule = [node("events")]  # no forbid list: the anchor alone
    assert validate_rules(rule) == []
    free = find_paths(G, t("sessions"), t("orders"), cfg=CFG)
    assert ["sessions", "events", "orders"] in _route(free.paths)
    res = find_paths(
        G,
        t("sessions"),
        t("orders"),
        cfg=CFG,
        automaton=compile_rules(rule, t("sessions"), t("orders")),
    )
    assert _route(res.paths) == [["sessions", "orders"]]
    # Searches from or to the node itself are allowed.
    for a, b in (("events", "payments"), ("payments", "events")):
        auto = compile_rules(rule, t(a), t(b))
        assert not auto.is_empty()
        res = find_paths(G, t(a), t(b), cfg=CFG, automaton=auto)
        assert res.refusal is None and res.paths, (a, b)


def test_node_rule_forbid_list_adds_intermediates() -> None:
    # The loader's form: a bare table reference in the forbid list.
    rule = [node("events", forbid=["shop.orders"])]
    assert validate_rules(rule) == []
    auto = compile_rules(rule, t("sessions"), t("payments"))
    res = find_paths(G, t("sessions"), t("payments"), cfg=CFG, automaton=auto)
    assert res.refusal is Refusal.RULE_REJECTS_ALL  # every route passes orders
    # Withheld per endpoint: both forbidden nodes are the endpoints here.
    auto = compile_rules(rule, t("events"), t("orders"))
    assert not auto.is_empty()
    assert _route(find_paths(G, t("events"), t("orders"), cfg=CFG, automaton=auto).paths)[0] == [
        "events",
        "orders",
    ]


def test_node_forbid_withheld_at_endpoint() -> None:
    rules = [node("sessions", forbid=["node(shop.sessions)"])]
    auto = compile_rules(rules, t("events"), t("sessions"))
    assert not auto.is_empty()
    res = find_paths(G, t("events"), t("sessions"), cfg=CFG, automaton=auto)
    assert res.refusal is None and _route(res.paths)[0] == ["events", "sessions"]
    # A forbidden node that is an endpoint is withheld even under another anchor.
    rules = [node("payments", forbid=["node(shop.orders)"])]
    assert not compile_rules(rules, t("events"), t("orders")).is_empty()
    # Pair scope is not withheld: forbidding your own endpoint is a contradiction.
    auto = compile_rules(
        [pair("events", "orders", forbid=["node(shop.orders)"])], t("events"), t("orders")
    )
    assert auto.is_empty()


@pytest.mark.parametrize(
    "forbid", ["any", "col(shop.orders.session_id)", VIA, "node(shop.x) | col(shop.a.b)"]
)
def test_node_rule_forbids_only_nodes(forbid: str) -> None:
    (d,) = validate_rules([node("events", forbid=[forbid])])
    assert d.code == "SG602" and "only forbid nodes" in d.message
    # Fail closed: it governs every search.
    assert compile_rules([node("events", forbid=[forbid])], t("orders"), t("payments")).is_empty()


@pytest.mark.parametrize(
    ("require", "forbid"),
    [
        ([VIA], [VIA]),
        ([VIA], ["col(shop.sessions.session_id)"]),
        ([VIA], ["node(shop.sessions)"]),
        (["col(shop.orders.session_id)"], ["node(shop.orders)"]),  # orders is an endpoint
        (["col(shop.payments.order_id)"], ["node(shop.payments)"]),
        (["node(shop.sessions)"], ["node(shop.sessions)"]),
        (["any"], ["any"]),
        ([], ["any"]),
    ],
)
def test_unsatisfiable_detected_offline(require: list[str], forbid: list[str]) -> None:
    auto = compile_rules(
        [pair("events", "orders", require=require, forbid=forbid)], t("events"), t("orders")
    )
    assert auto.is_empty()
    res = find_paths(G, t("events"), t("orders"), cfg=CFG, automaton=auto)
    assert res.refusal is Refusal.WAYPOINT_UNSATISFIABLE and res.paths == ()


def test_satisfiable_not_flagged_empty_and_rule_rejects_all() -> None:
    # Satisfiable in the predicate language, but the graph has no such route.
    req = ["edge(shop.events.order_id->shop.payments.order_id)"]
    auto = compile_rules([pair("events", "orders", require=req)], t("events"), t("orders"))
    assert not auto.is_empty()
    res = find_paths(G, t("events"), t("orders"), cfg=CFG, automaton=auto)
    assert res.refusal is Refusal.RULE_REJECTS_ALL


def test_automaton_states_and_dead_state() -> None:
    auto = compile_rules(
        [
            pair(
                "events",
                "orders",
                require=[VIA, "node(shop.sessions)"],
                forbid=["col(shop.events.order_id)"],
            )
        ],
        t("events"),
        t("orders"),
    )
    hops = {(h.start.table, h.end.table): h for tab in G.tables for h in G.neighbours(tab)}
    s0 = auto.start
    assert not auto.accepting(s0)
    s1 = auto.step(s0, hops[("events", "sessions")])
    assert not auto.accepting(auto.step(s0, hops[("events", "orders")]))
    dead = auto.step(s0, hops[("events", "orders")])
    assert auto.step(dead, hops[("orders", "sessions")]) == dead  # dead is absorbing
    s2 = auto.step(s1, hops[("sessions", "orders")])
    assert auto.accepting(s2)
    hash(s2)
    # No rule for the pair: every route is accepted.
    free = compile_rules([], t("events"), t("orders"))
    assert free.accepting(free.start) and not free.is_empty()


# --- lint ---------------------------------------------------------------------------------------


def test_lint_dead_prohibition() -> None:
    edges = G.edges
    rules = [
        pair(
            "events",
            "orders",
            forbid=[
                "edge(shop.orders.order_id->shop.events.order_id)",  # wrong orientation: dead
                "edge(shop.events.order_id->shop.orders.order_id)",  # live
                "node(shop.nowhere)",  # dead
                "col(shop.orders.session_id)",  # live
                "col(shop.orders.nope)",  # dead
                "any",  # live
            ],
        ),
        node("sessions", forbid=["node(shop.sessions)"]),  # live
    ]
    diags = lint_rules(rules, edges)
    assert all(d.code == "SG604" and d.severity is Severity.WARNING for d in diags)
    assert all(d.location == "o.yaml:3" for d in diags)
    msgs = " | ".join(d.message for d in diags)
    assert len(diags) == 3
    assert "shop.orders.order_id->shop.events.order_id" in msgs
    assert "shop.nowhere" in msgs and "shop.orders.nope" in msgs
    # A malformed predicate is not linted (validate_rules reports it).
    assert lint_rules([pair("a", "b", forbid=["oops("])], edges) == []


def test_lint_dead_requirement_and_node_anchor() -> None:
    rules = [
        pair(
            "events",
            "orders",
            require=[
                "node(shop.sesions)",  # typo: dead
                VIA,  # live
                "edge(shop.x.a->shop.y.b) | node(shop.sessions)",  # one live alternative
                "col(shop.orders.nope) | node(shop.nowhere)",  # every alternative dead
            ],
        ),
        node("nowhere"),  # the anchor itself matches nothing
    ]
    diags = lint_rules(rules, G.edges)
    assert [d.code for d in diags] == ["SG604"] * 3
    msgs = [d.message for d in diags]
    assert any("prohibition node(shop.nowhere)" in m for m in msgs)
    assert any("requirement node(shop.sesions)" in m for m in msgs)
    assert any("requirement col(shop.orders.nope) | node(shop.nowhere)" in m for m in msgs)


def test_codes_registered() -> None:
    doc = (Path(__file__).resolve().parents[3] / "docs/reference/diagnostics.md").read_text(
        encoding="utf-8"
    )
    for code in ("SG602", "SG603", "SG604", "SG605"):
        assert code in CODES and f"| {code} |" in doc
