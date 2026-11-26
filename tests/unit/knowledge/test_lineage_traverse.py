"""Lineage traversal (0.2.0 spec 4.8): breadth-first, bounded, cycle-safe, deterministic."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from schemaguru.errors import SchemaGuruError
from schemaguru.knowledge.lineage import MAX_NODES, LineageResult, traverse
from schemaguru.model.facts import Fact, Provenance, Status
from schemaguru.model.kinds import FactKind
from schemaguru.model.lineage import column_lineage_value, table_lineage_value
from schemaguru.model.refs import ObjectRef
from schemaguru.render.lineage import lineage_text
from schemaguru.store.protocol import lineage_index
from schemaguru.store.sqlite import SQLiteStore

DBT = "dbt:manifest:model.analytics.revenue_daily"
VIEW = "view_definition"
REVENUE = ObjectRef(None, "analytics", "revenue_daily")


def ref(text: str) -> ObjectRef:
    return ObjectRef.parse(text, has_column=text.count(".") == 2)


def lin(
    subject: str,
    upstream: list[str],
    source: str = VIEW,
    *,
    unresolved: tuple[str, ...] = (),
    prov: Provenance = Provenance.CATALOG,
) -> Fact:
    value = table_lineage_value(upstream, unresolved)
    return Fact.make(
        FactKind.LINEAGE, ObjectRef.parse(subject), value, prov, Status.UNVERIFIED, source=source
    )


def col_lin(subject: str, inputs: list[str], transform: str, source: str = VIEW) -> Fact:
    value = column_lineage_value(inputs, transform)
    return Fact.make(
        FactKind.COLUMN_LINEAGE, ref(subject), value, Provenance.CATALOG, Status.UNVERIFIED,
        source=source,
    )  # fmt: skip


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SQLiteStore]:
    s = SQLiteStore(tmp_path / "s.db")
    yield s
    s.close()


def _revenue(store: SQLiteStore) -> None:
    store.put_facts(
        [
            lin("analytics.revenue_daily", ["shop.orders", "shop.payments"], DBT),
            lin("analytics.revenue_daily", ["shop.payments"]),
            lin("shop.payments", [], unresolved=("raw.payments",)),
            lin("raw.payments", ["raw.never_followed"]),  # an unresolved leaf is not expanded
        ]
    )


def test_upstream_depth_two(store: SQLiteStore) -> None:
    _revenue(store)
    store.put_facts([lin("shop.orders", ["stage.orders"]), lin("stage.orders", ["raw.orders"])])
    r = traverse(store, REVENUE)
    assert (r.direction, r.level, r.depth, r.truncated, r.cycles) == (
        "upstream", "table", 2, False, ()
    )  # fmt: skip
    assert [(n.ref, n.depth) for n in r.nodes] == [
        ("analytics.revenue_daily", 0), ("shop.orders", 1), ("shop.payments", 1),
        ("stage.orders", 2), ("raw.payments", 2),
    ]  # fmt: skip
    assert lineage_text(r) == "\n".join(
        [
            "Lineage upstream of analytics.revenue_daily (table level, depth 2) — "
            "declared/parsed, unverified; not a join path",
            "  analytics.revenue_daily <- shop.orders [dbt:manifest]",
            "  analytics.revenue_daily <- shop.payments [dbt:manifest, view_definition]",
            "  shop.orders <- stage.orders [view_definition]",
            "  shop.payments <- raw.payments (not in catalog) [view_definition]",
        ]
    )
    assert "raw.orders" not in {n.ref for n in r.nodes}  # depth 3 is not walked
    d = r.to_dict()
    assert set(d) == {"ref", "direction", "level", "depth", "nodes", "edges", "truncated",
                      "cycles"}  # fmt: skip
    assert d["edges"][1] == {
        "upstream": "shop.payments", "downstream": "analytics.revenue_daily",
        "source": ["dbt:manifest", "view_definition"], "transform": None,
    }  # fmt: skip
    assert d["nodes"][0] == {
        "ref": "analytics.revenue_daily", "level": "table", "depth": 0, "in_catalog": True
    }  # fmt: skip
    # negative control: depth 3 reaches it
    assert "raw.orders" in {n.ref for n in traverse(store, REVENUE, depth=3).nodes}


def test_downstream_uses_index(store: SQLiteStore, monkeypatch: pytest.MonkeyPatch) -> None:
    _revenue(store)
    store.put_facts([lin("analytics.dashboard", ["analytics.revenue_daily"])])
    calls: list[str] = []
    real = store.lineage_edges

    def spy(*a: Any, **k: Any) -> Any:
        calls.append(k["direction"])
        return real(*a, **k)

    def no_scan() -> Any:
        raise AssertionError("downstream lineage must not scan every fact")

    monkeypatch.setattr(store, "lineage_edges", spy)
    monkeypatch.setattr(store, "iter_facts", no_scan)
    r = traverse(store, ObjectRef(None, "shop", "payments"), direction="downstream")
    assert calls == ["downstream", "downstream"]
    assert [(e.upstream, e.downstream, e.sources) for e in r.edges] == [
        ("shop.payments", "analytics.revenue_daily", ("dbt:manifest", "view_definition")),
        ("analytics.revenue_daily", "analytics.dashboard", ("view_definition",)),
    ]
    assert lineage_text(r).splitlines()[0].startswith("Lineage downstream of shop.payments")


def test_column_level_inferred(store: SQLiteStore) -> None:
    _revenue(store)
    store.put_facts(
        [col_lin("analytics.revenue_daily.net_revenue", ["shop.orders.amount"], "aggregate")]
    )
    column = ObjectRef(None, "analytics", "revenue_daily", "net_revenue")
    r = traverse(store, column)
    assert r.level == "column" and r.root == "analytics.revenue_daily.net_revenue"
    assert [(e.upstream, e.transform) for e in r.edges] == [("shop.orders.amount", "aggregate")]
    assert lineage_text(r).splitlines()[1] == (
        "  analytics.revenue_daily.net_revenue <- shop.orders.amount (aggregate) [view_definition]"
    )
    down = traverse(store, ObjectRef(None, "shop", "orders", "amount"), direction="downstream")
    assert [e.downstream for e in down.edges] == ["analytics.revenue_daily.net_revenue"]
    # level="table" on a column walks its table
    t = traverse(store, column, level="table")
    assert t.level == "table" and t.root == "analytics.revenue_daily"
    assert {e.upstream for e in t.edges} >= {"shop.orders", "shop.payments"}
    with pytest.raises(ValueError, match="column"):
        traverse(store, REVENUE, level="column")


def test_cycle_reported_terminates(store: SQLiteStore) -> None:
    human = Provenance.HUMAN
    store.put_facts(
        [
            lin("shop.a", ["shop.b"], "overlay:lineage.yml:2", prov=human),
            lin("shop.b", ["shop.a"], "overlay:lineage.yml:4", prov=human),
        ]
    )
    r = traverse(store, ObjectRef(None, "shop", "a"), depth=5)
    assert [n.ref for n in r.nodes] == ["shop.a", "shop.b"]
    assert r.cycles == (("shop.a", "shop.b", "shop.a"),)
    assert len(r.edges) == 2
    assert lineage_text(r).splitlines()[-1] == "cycle: shop.a -> shop.b -> shop.a"
    down = traverse(store, ObjectRef(None, "shop", "a"), direction="downstream", depth=5)
    assert down.cycles == (("shop.a", "shop.b", "shop.a"),)


def test_cap_200_truncates(store: SQLiteStore) -> None:
    store.put_facts([lin("shop.hub", [f"shop.t{i:03d}" for i in range(300)])])
    r = traverse(store, ObjectRef(None, "shop", "hub"))
    assert r.truncated and len(r.nodes) == MAX_NODES == 200
    assert len(r.edges) == MAX_NODES - 1
    assert lineage_text(r).splitlines()[-1] == "(truncated at 200 nodes)"
    assert r.to_dict()["truncated"] is True
    small = traverse(store, ObjectRef(None, "shop", "hub"), max_nodes=400)
    assert not small.truncated and len(small.nodes) == 301  # the control


def test_depth_bounds(store: SQLiteStore) -> None:
    for bad in (0, 6, -1):
        with pytest.raises(ValueError, match="depth"):
            traverse(store, REVENUE, depth=bad)
    for ok in (1, 5):
        assert traverse(store, REVENUE, depth=ok).depth == ok
    with pytest.raises(ValueError, match="direction"):
        traverse(store, REVENUE, direction="sideways")  # type: ignore[arg-type]


class _CustomStore:
    """A Store without the lineage index (a custom Store)."""

    def __init__(self, inner: SQLiteStore) -> None:
        self._inner = inner

    def facts_for(self, subjects: Any, kinds: Any) -> Any:
        return self._inner.facts_for(subjects, kinds)


def test_custom_store_downstream_sg705(store: SQLiteStore) -> None:
    _revenue(store)
    custom: Any = _CustomStore(store)
    assert lineage_index(custom) is None
    up = traverse(custom, REVENUE)  # upstream needs no index
    assert {e.upstream for e in up.edges} == {"shop.orders", "shop.payments", "raw.payments"}
    with pytest.raises(SchemaGuruError, match="SG705"):
        traverse(custom, ObjectRef(None, "shop", "orders"), direction="downstream")


def test_unresolved_leaf(store: SQLiteStore) -> None:
    _revenue(store)
    r = traverse(store, ObjectRef(None, "shop", "payments"), depth=5)
    assert [(n.ref, n.in_catalog) for n in r.nodes] == [
        ("shop.payments", True), ("raw.payments", False)
    ]  # fmt: skip
    assert "raw.never_followed" not in {n.ref for n in r.nodes}


def test_deterministic_order(tmp_path: Path) -> None:
    facts = [
        lin("analytics.revenue_daily", ["shop.orders", "shop.payments"], DBT),
        lin("analytics.revenue_daily", ["shop.payments"]),
        lin("shop.payments", ["raw.p"], unresolved=("raw.q",)),
        lin("shop.orders", ["raw.o", "raw.a"]),
        lin("analytics.weekly", ["analytics.revenue_daily"]),
    ]
    out: list[LineageResult] = []
    for i, order in enumerate((facts, facts[::-1])):
        s = SQLiteStore(tmp_path / f"{i}.db")
        for f in order:
            s.put_facts([f])
        out.append(traverse(s, REVENUE, depth=3))
        s.close()
    assert out[0] == out[1]
    assert out[0].to_dict() == out[1].to_dict()


def test_sibling_cycle_reported_both_directions(store: SQLiteStore) -> None:
    """Siblings at the same depth that feed each other are a cycle even though neither is on
    the other's discovery chain (R <- B, R <- C, B <- C, C <- B; and the mirror downstream)."""
    store.put_facts(
        [
            lin("shop.r", ["shop.b", "shop.c"]),
            lin("shop.b", ["shop.c", "shop.x"]),
            lin("shop.c", ["shop.b", "shop.x"]),
        ]
    )
    up = traverse(store, ObjectRef(None, "shop", "r"), depth=5)
    assert up.cycles == (("shop.b", "shop.c", "shop.b"),)
    assert "cycle: shop.b -> shop.c -> shop.b" in lineage_text(up)
    down = traverse(store, ObjectRef(None, "shop", "x"), direction="downstream", depth=5)
    assert down.cycles == (("shop.b", "shop.c", "shop.b"),)
    # the control: without the sibling links there is no cycle
    store.put_facts([lin("shop.q", ["shop.r"])])
    assert traverse(store, ObjectRef(None, "shop", "q"), depth=1).cycles == ()
