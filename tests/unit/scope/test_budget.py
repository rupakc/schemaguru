from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from schemaguru.config import ScopeConfig
from schemaguru.model.facts import Fact, Provenance, Status
from schemaguru.model.joins import JoinRef
from schemaguru.model.kinds import FactKind
from schemaguru.model.refs import ObjectRef
from schemaguru.model.structure import Column, Constraint
from schemaguru.scope import (
    DetailLevel,
    Scope,
    TableRenderer,
    build_scope,
    fit,
    lite_columns,
)
from schemaguru.scope.tokens import CharTokenCounter
from schemaguru.store.sqlite import SQLiteStore

NOW = datetime(2026, 11, 24, tzinfo=timezone.utc)
SIZE = {
    DetailLevel.FULL: 400,
    DetailLevel.STANDARD: 200,
    DetailLevel.LITE: 80,
    DetailLevel.NAME_ONLY: 20,
}


def t(name: str) -> ObjectRef:
    return ObjectRef(None, "shop", name)


class FakeRenderer:
    """Renders ``level`` as a block of ``SIZE[level]`` characters (= SIZE/4 tokens)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, DetailLevel]] = []

    def render(self, ref: ObjectRef, level: DetailLevel) -> str:
        self.calls.append((ref.table, level))
        return "x" * SIZE[level]


def tok(level: DetailLevel) -> int:
    return SIZE[level] // 4


def levels(out: Sequence[tuple[ObjectRef, DetailLevel]]) -> list[tuple[str, DetailLevel]]:
    return [(r.table, lv) for r, lv in out]


def test_levels_ordered_richest_first() -> None:
    assert list(DetailLevel) == [
        DetailLevel.FULL,
        DetailLevel.STANDARD,
        DetailLevel.LITE,
        DetailLevel.NAME_ONLY,
    ]


def test_fit_everything_full_when_it_fits() -> None:
    r: TableRenderer = FakeRenderer()
    ranked = [t("a"), t("b")]
    out = fit(ranked, r, 2 * tok(DetailLevel.FULL), CharTokenCounter())
    assert levels(out) == [("a", DetailLevel.FULL), ("b", DetailLevel.FULL)]


def test_fit_degrades_detail_not_tables_first() -> None:
    ranked = [t("a"), t("b"), t("c"), t("d")]
    # room for one FULL + three NAME_ONLY, nowhere near four FULL
    budget = tok(DetailLevel.FULL) + 3 * tok(DetailLevel.NAME_ONLY)
    out = fit(ranked, FakeRenderer(), budget, CharTokenCounter())
    assert [r for r, _ in out] == ranked  # every table kept ...
    assert levels(out) == [  # ... the top-ranked richest, the rest degraded
        ("a", DetailLevel.FULL),
        ("b", DetailLevel.NAME_ONLY),
        ("c", DetailLevel.NAME_ONLY),
        ("d", DetailLevel.NAME_ONLY),
    ]
    # a bit more room goes to the next table in rank order
    more = budget + tok(DetailLevel.LITE) - tok(DetailLevel.NAME_ONLY)
    assert levels(fit(ranked, FakeRenderer(), more, CharTokenCounter()))[1] == (
        "b",
        DetailLevel.LITE,
    )
    total = sum(
        CharTokenCounter().count(FakeRenderer().render(r, lv))
        for r, lv in fit(ranked, FakeRenderer(), more, CharTokenCounter())
    )
    assert total <= more


def test_fit_drops_lowest_ranked_only_when_names_do_not_fit() -> None:
    ranked = [t("a"), t("b"), t("c")]
    out = fit(ranked, FakeRenderer(), 2 * tok(DetailLevel.NAME_ONLY), CharTokenCounter())
    assert levels(out) == [("a", DetailLevel.NAME_ONLY), ("b", DetailLevel.NAME_ONLY)]
    assert fit(ranked, FakeRenderer(), 0, CharTokenCounter()) == []
    assert fit([], FakeRenderer(), 100, CharTokenCounter()) == []
    # a duplicate in the ranking is rendered once
    assert levels(fit([t("a"), t("a")], FakeRenderer(), 1000, CharTokenCounter())) == [
        ("a", DetailLevel.FULL)
    ]


def test_fit_level_that_is_not_smaller_is_skipped() -> None:
    class Odd:
        def render(self, ref: ObjectRef, level: DetailLevel) -> str:
            return "y" * (40 if level is DetailLevel.NAME_ONLY else 400)

    out = fit([t("a")], Odd(), 10, CharTokenCounter())
    assert levels(out) == [("a", DetailLevel.NAME_ONLY)]


# ----- LITE column priority --------------------------------------------------------------


def col(name: str, ordinal: int, comment: str | None = None) -> Column:
    return Column(ObjectRef(None, "shop", "orders", name), ordinal, "T", "string", None, comment)


ORDERS = [
    col("order_id", 1),
    col("customer_id", 2),
    col("status", 3),
    col("created_at", 4),
    col("note", 5),
    col("deleted", 6),
    col("channel", 7, comment="sales channel"),
    col("session_ref", 8),
]
PK = Constraint("primary_key", t("orders"), ("order_id",), None, None, True)
FK = Constraint("foreign_key", t("orders"), ("customer_id",), t("customers"), ("customer_id",))


def soft_delete(pred: str, markers: list[str] | None = None) -> Fact:
    return Fact.make(
        FactKind.SOFT_DELETE,
        t("orders"),
        {"predicate": pred, "marker_columns": markers},
        Provenance.HUMAN,
        Status.UNVERIFIED,
    )


def test_lite_keeps_soft_delete_column() -> None:
    facts = [soft_delete("(deleted IS NULL OR NOT deleted)")]
    names = [c.ref.column for c in lite_columns(ORDERS, [PK, FK], facts, limit=3)]
    assert names == ["order_id", "customer_id", "deleted"]
    # without the soft-delete fact, 'deleted' is just one of the rest
    rest = [c.ref.column for c in lite_columns(ORDERS, [PK, FK], [], limit=3)]
    assert rest == ["order_id", "customer_id", "channel"]


def test_lite_priority_exactly_as_spec() -> None:
    join = Fact.make(
        FactKind.JOIN,
        JoinRef(
            (ObjectRef(None, "shop", "events", "ref"),),
            (ObjectRef(None, "shop", "orders", "session_ref"),),
        ),
        {"match_rate": 1.0},
        Provenance.MEASURED,
        Status.OBSERVED,
        method="exact",
        observed_at=NOW,
    )
    pitfall = Fact.make(
        FactKind.PITFALL,
        t("orders"),
        {"rule": "Status is free text", "fix": "use STATUS codes", "severity": "warn"},
        Provenance.HUMAN,
        Status.UNVERIFIED,
    )
    values = Fact.make(
        FactKind.VALUES,
        ObjectRef(None, "shop", "orders", "note"),
        {"values": ["a"], "case_sensitive": True, "split": None},
        Provenance.HUMAN,
        Status.UNVERIFIED,
    )
    markers = soft_delete("is_gone = 1", ["deleted"])
    names = [
        c.ref.column
        for c in lite_columns(ORDERS, [PK, FK], [join, pitfall, values, markers], limit=99)
    ]
    assert names == [
        "order_id",  # PK
        "customer_id",  # FK (constraint) ...
        "session_ref",  # ... and join columns (measured), by ordinal
        "status",  # referenced by a pitfall (whole word, any case)
        "deleted",  # soft-delete marker column
        "note",  # values
        "channel",  # description (comment)
        "created_at",  # the rest, by ordinal
    ]


def test_lite_pk_from_fact_and_column_description_fact() -> None:
    pk = Fact.make(
        FactKind.PRIMARY_KEY,
        t("orders"),
        {"columns": ["note"]},
        Provenance.HUMAN,
        Status.UNVERIFIED,
    )
    desc = Fact.make(
        FactKind.DESCRIPTION,
        ObjectRef(None, "shop", "orders", "created_at"),
        {"text": "when placed"},
        Provenance.DRAFTED,
        Status.UNVERIFIED,
    )
    names = [c.ref.column for c in lite_columns(ORDERS, [], [pk, desc], limit=3)]
    assert names == ["note", "created_at", "channel"]
    assert lite_columns(ORDERS, [], [], limit=0) == []


# ----- the scope pipeline ----------------------------------------------------------------


class Ranked:
    def __init__(self, names: Sequence[str]) -> None:
        self.names = names

    def search(self, question: str, k: int) -> list[tuple[ObjectRef, float]]:
        return [(t(n), float(100 - i)) for i, n in enumerate(self.names[:k])]


def test_not_shown_count(shop_store: SQLiteStore) -> None:
    names = ["orders", "customers", "payments", "events", "sessions", "products", "accounts"]
    cfg = replace(ScopeConfig(), seed_k=3)
    budget = 3 * tok(DetailLevel.FULL)
    sc = build_scope(
        "q",
        retriever=Ranked(names),
        store=shop_store,
        graph=None,
        renderer=FakeRenderer(),
        counter=CharTokenCounter(),
        budget_tokens=budget,
        cfg=cfg,
    )
    assert isinstance(sc, Scope)
    assert [r.table for r, _ in sc.tables] == ["orders", "customers", "payments"]
    assert sc.not_shown == 4  # the other candidates the retriever found
    # tables dropped by the budget are "not shown" too
    tiny = build_scope(
        "q",
        retriever=Ranked(names),
        store=shop_store,
        graph=None,
        renderer=FakeRenderer(),
        counter=CharTokenCounter(),
        budget_tokens=tok(DetailLevel.NAME_ONLY),
        cfg=cfg,
    )
    assert [r.table for r, _ in tiny.tables] == ["orders"]
    assert tiny.not_shown == 6
    assert tiny.question == "q"


def test_scope_marks_unmeasured(shop_store: SQLiteStore) -> None:
    shop_store.put_facts(
        [
            Fact.make(
                FactKind.ROW_COUNT,
                t("orders"),
                {"rows": 5},
                Provenance.MEASURED,
                Status.OBSERVED,
                method="exact",
                observed_at=NOW,
            )
        ]
    )
    sc = build_scope(
        "q",
        retriever=Ranked(["orders", "customers"]),
        store=shop_store,
        graph=None,
        renderer=FakeRenderer(),
        counter=CharTokenCounter(),
        budget_tokens=10_000,
        cfg=ScopeConfig(),
    )
    assert sc.unmeasured == (t("customers"),)
    assert sc.not_shown == 0


def test_scope_includes_bridges_after_seeds(shop_store: SQLiteStore) -> None:
    from tests.unit.graph.shop_graph import shop_facts
    from tests.unit.scope.test_expand import _graph

    sc = build_scope(
        "q",
        retriever=Ranked(["payments", "customers"]),
        store=shop_store,
        graph=_graph(shop_facts()),
        renderer=FakeRenderer(),
        counter=CharTokenCounter(),
        budget_tokens=10_000,
        cfg=ScopeConfig(),
    )
    assert [r.table for r, _ in sc.tables] == ["payments", "customers", "orders"]


def test_scope_rejects_negative_budget(shop_store: SQLiteStore) -> None:
    with pytest.raises(ValueError):
        build_scope(
            "q",
            retriever=Ranked([]),
            store=shop_store,
            graph=None,
            renderer=FakeRenderer(),
            counter=CharTokenCounter(),
            budget_tokens=-1,
            cfg=ScopeConfig(),
        )


def test_lite_ignores_other_tables_and_nested_pitfall_values() -> None:
    other = Fact.make(
        FactKind.PITFALL,
        t("customers"),
        {"rule": "channel is wrong", "fix": None, "severity": "warn"},
        Provenance.HUMAN,
        Status.UNVERIFIED,
    )
    nested = Fact.make(
        FactKind.PITFALL,
        t("orders"),
        {"rule": "see notes", "fix": {"steps": ["filter on created_at"]}, "severity": "warn"},
        Provenance.HUMAN,
        Status.UNVERIFIED,
    )
    names = [c.ref.column for c in lite_columns(ORDERS, [], [other, nested], limit=1)]
    assert names == ["created_at"]
