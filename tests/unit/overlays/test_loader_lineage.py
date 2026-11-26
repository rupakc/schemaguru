from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from schemaguru.catalog.crawl import crawl
from schemaguru.diagnostics import Severity
from schemaguru.lineage.parse import lineage_available
from schemaguru.model.facts import Fact, Provenance, Status
from schemaguru.model.kinds import LINEAGE_KINDS, FactKind
from schemaguru.model.lineage import VIEW_SOURCE, column_lineage_value, table_lineage_value
from schemaguru.model.refs import ObjectRef
from schemaguru.model.structure import Column, Table
from schemaguru.overlays.loader import (
    lineage_ignore_diagnostics,
    load_overlays,
    retract_removed,
)
from schemaguru.store.sqlite import SQLiteStore
from tests.unit.catalog.fake_reader import ViewFakeReader

from .conftest import FIXTURES

TODAY = date(2026, 6, 1)
REVENUE = ObjectRef(None, "analytics", "revenue_daily")
LEGACY = ObjectRef(None, "analytics", "legacy_view")


def _add(store: SQLiteStore, schema: str, table: str, cols: list[str]) -> None:
    ref = ObjectRef(None, schema, table)
    store.upsert_tables([Table(ref, "table")])
    store.upsert_columns(
        ref,
        [
            Column(ObjectRef(None, schema, table, c), i, "STRING", "STRING", True, None, None)
            for i, c in enumerate(cols, 1)
        ],
    )


@pytest.fixture
def lstore(store: SQLiteStore) -> SQLiteStore:
    _add(store, "shop", "refunds", ["order_id", "amount"])
    _add(store, "analytics", "revenue_daily", ["day", "net_revenue"])
    _add(store, "analytics", "legacy_view", ["order_id"])
    return store


def _write(tmp_path: Path, body: str, name: str = "o.yaml") -> Path:
    p = tmp_path / name
    p.write_text("version: 1\nlineage:\n" + body, encoding="utf-8")
    return p


def _parsed(subject: ObjectRef, upstream: list[str]) -> Fact:
    return Fact.make(
        FactKind.LINEAGE, subject, table_lineage_value(upstream), Provenance.CATALOG,
        Status.UNVERIFIED, source=VIEW_SOURCE,
    )  # fmt: skip


def _facts(store: SQLiteStore, ref: ObjectRef = REVENUE) -> list[Fact]:
    return store.facts_for([ref], LINEAGE_KINDS)


def _edges(store: SQLiteStore, ref: ObjectRef = REVENUE) -> list[str]:
    es = store.lineage_edges([ref.canonical()], direction="upstream", level="table")
    return sorted(e.upstream_key for e in es)


def test_spec_example_loads(lstore: SQLiteStore) -> None:
    facts, _, _, _, bag = load_overlays(
        [FIXTURES / "overlays" / "lineage.yaml"], lstore, today=TODAY
    )
    assert list(bag) == []
    assert len(facts) == 3
    assert all(f.provenance is Provenance.HUMAN and f.status is Status.UNVERIFIED for f in facts)
    by_line = {int(f.source.rsplit(":", 1)[1]): f for f in facts if f.source}
    assert sorted(by_line) == [3, 5, 8]
    assert all(f.source.startswith("overlay:") for f in facts if f.source)
    t, c, i = by_line[3], by_line[5], by_line[8]
    assert t.kind.name == "LINEAGE" and t.value["upstream"] == ["shop.orders", "shop.refunds"]
    assert c.kind.name == "COLUMN_LINEAGE" and c.value["transform"] == "aggregate"
    assert c.value["inputs"] == ["shop.orders.amount", "shop.refunds.amount"]
    assert i.value["ignore"] is True and i.value["upstream"] == ["shop.orders"]


def test_column_transform_defaults_to_unknown(lstore: SQLiteStore, tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        "  - column: analytics.revenue_daily.net_revenue\n    inputs: [shop.orders.amount]\n",
    )
    facts, *_, bag = load_overlays([p], lstore, today=TODAY)
    assert list(bag) == [] and facts[0].value["transform"] == "unknown"


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ("  - upstream: [shop.orders]\n", "SG208"),  # neither table nor column
        (
            "  - table: analytics.revenue_daily\n    column: analytics.revenue_daily.day\n"
            "    upstream: [shop.orders]\n",
            "SG208",
        ),  # both
        (
            "  - column: analytics.revenue_daily.day\n    upstream: [shop.orders]\n",
            "SG208",
        ),  # upstream with column
        (
            "  - table: analytics.revenue_daily\n    inputs: [shop.orders.amount]\n",
            "SG208",
        ),  # inputs with table
        (
            "  - column: analytics.revenue_daily.day\n    inputs: [shop.orders.amount]\n"
            "    transform: sum\n",
            "SG208",
        ),  # bad transform
        (
            "  - table: analytics.revenue_daily\n    upstream: [shop.orders]\n"
            "    transform: copy\n",
            "SG208",
        ),  # transform on a table entry
        (
            "  - table: analytics.revenue_daily\n    upstream: [shop.orders]\n    ignore: maybe\n",
            "SG208",
        ),  # ignore not a bool
        ("  - table: analytics.revenue_daily\n", "SG208"),  # non-ignore without a list
        ("  - table: analytics.revenue_daily\n    upstream: []\n", "SG208"),  # empty list
        ("  - table: analytics.revenue_daily\n    upstream: shop.orders\n", "SG208"),  # bare str
        ("  - table: analytics.nope\n    upstream: [shop.orders]\n", "SG203"),
        ("  - table: analytics.revenue_daily\n    upstream: [shop.nope]\n", "SG203"),
        (
            "  - column: analytics.revenue_daily.net_revenue\n    inputs: [shop.orders.nope]\n",
            "SG203",
        ),
        ("  - column: analytics.revenue_daily.nope\n    inputs: [shop.orders.amount]\n", "SG203"),
        (
            "  - table: analytics.revenue_daily\n    upstream: [shop.orders]\n    bogus: 1\n",
            "SG202",
        ),
    ],
)
def test_validation_errors(lstore: SQLiteStore, tmp_path: Path, body: str, code: str) -> None:
    facts, *_, bag = load_overlays([_write(tmp_path, body)], lstore, today=TODAY)
    assert code in [d.code for d in bag]
    if code != "SG202":
        assert facts == []  # the entry is dropped, never half-applied


def test_ambiguous_ref_is_sg204(lstore: SQLiteStore, tmp_path: Path) -> None:
    _add(lstore, "shop", "Orders", ["amount"])
    _add(lstore, "shop", "ORDERS", ["amount"])
    p = _write(tmp_path, "  - table: analytics.revenue_daily\n    upstream: [shop.oRders]\n")
    facts, *_, bag = load_overlays([p], lstore, today=TODAY)
    assert [d.code for d in bag] == ["SG204"] and facts == []


def test_human_entry_replaces_parsed_edges(lstore: SQLiteStore, tmp_path: Path) -> None:
    lstore.put_facts([_parsed(REVENUE, ["shop.customers"])])
    assert _edges(lstore) == ["shop.customers"]
    p = _write(tmp_path, "  - table: analytics.revenue_daily\n    upstream: [shop.orders]\n")
    facts, *_, _bag = load_overlays([p], lstore, today=TODAY)
    lstore.put_facts(facts)
    assert _edges(lstore) == ["shop.orders"]


def test_ignore_hides_link_and_is_kept_in_store(lstore: SQLiteStore, tmp_path: Path) -> None:
    parsed = _parsed(LEGACY, ["shop.orders"])
    lstore.put_facts([parsed])
    p = _write(
        tmp_path,
        "  - table: analytics.legacy_view\n    upstream: [shop.orders]\n    ignore: true\n",
    )
    facts, *_ = load_overlays([p], lstore, today=TODAY)
    lstore.put_facts(facts)
    assert _edges(lstore, LEGACY) == []
    assert parsed.id in {f.id for f in _facts(lstore, LEGACY)}  # hidden, not deleted


def test_ignore_without_list_hides_all_parsed(lstore: SQLiteStore, tmp_path: Path) -> None:
    lstore.put_facts([_parsed(LEGACY, ["shop.orders"]), _parsed(LEGACY, ["shop.refunds"])])
    p = _write(tmp_path, "  - table: analytics.legacy_view\n    ignore: true\n")
    facts, *_, bag = load_overlays([p], lstore, today=TODAY)
    assert list(bag) == [] and facts[0].value["upstream"] is None
    lstore.put_facts(facts)
    assert _edges(lstore, LEGACY) == []
    assert lineage_ignore_diagnostics(lstore, facts) == []


def test_unmatched_ignore_sg213(lstore: SQLiteStore, tmp_path: Path) -> None:
    lstore.put_facts([_parsed(LEGACY, ["shop.customers"])])
    p = _write(
        tmp_path,
        "  - table: analytics.legacy_view\n    upstream: [shop.orders]\n    ignore: true\n",
    )
    facts, *_ = load_overlays([p], lstore, today=TODAY)
    lstore.put_facts(facts)
    diags = lineage_ignore_diagnostics(lstore, facts)
    assert [d.code for d in diags] == ["SG213"]
    assert diags[0].severity is Severity.WARNING
    assert diags[0].location == f"{p}:3"
    assert diags[0].message == "lineage ignore entry matches no parsed link"
    assert _edges(lstore, LEGACY) == ["shop.customers"]  # a mismatched ignore hides nothing


def test_table_and_column_levels_are_independent(lstore: SQLiteStore, tmp_path: Path) -> None:
    col = ObjectRef(None, "analytics", "revenue_daily", "net_revenue")
    parsed_col = Fact.make(
        FactKind.COLUMN_LINEAGE, col,
        column_lineage_value(["shop.orders.amount"], "copy"),
        Provenance.CATALOG, Status.UNVERIFIED, source=VIEW_SOURCE,
    )  # fmt: skip
    lstore.put_facts([_parsed(REVENUE, ["shop.orders"]), parsed_col])
    p = _write(tmp_path, "  - table: analytics.revenue_daily\n    ignore: true\n")
    facts, *_ = load_overlays([p], lstore, today=TODAY)
    lstore.put_facts(facts)
    assert _edges(lstore) == []
    col_edges = lstore.lineage_edges([col.canonical()], direction="upstream", level="column")
    assert [e.upstream_key for e in col_edges] == ["shop.orders.amount"]


def test_removed_entry_retracted_and_edges_restored(lstore: SQLiteStore, tmp_path: Path) -> None:
    lstore.put_facts([_parsed(REVENUE, ["shop.customers"])])
    p = _write(tmp_path, "  - table: analytics.revenue_daily\n    upstream: [shop.orders]\n")
    facts, *_, bag = load_overlays([p], lstore, today=TODAY)
    lstore.put_facts(facts)
    retract_removed(lstore, facts, bag.failed_files)
    assert _edges(lstore) == ["shop.orders"]
    p.write_text("version: 1\nlineage:\n", encoding="utf-8")
    facts, *_, bag = load_overlays([p], lstore, today=TODAY)
    assert retract_removed(lstore, facts, bag.failed_files) == 1
    assert _edges(lstore) == ["shop.customers"]


def test_identical_lines_share_one_fact_and_one_removal_keeps_it(
    lstore: SQLiteStore, tmp_path: Path
) -> None:
    entry = "  - table: analytics.revenue_daily\n    upstream: [shop.refunds, shop.orders]\n"
    other = "  - table: analytics.revenue_daily\n    upstream: [shop.orders, shop.refunds]\n"
    p = _write(tmp_path, entry + other)
    facts, *_, bag = load_overlays([p], lstore, today=TODAY)
    assert list(bag) == []
    assert len({f.id for f in facts}) == 1 and len(facts) == 1  # one fact, first line's source
    assert facts[0].source == f"overlay:{p}:3"
    lstore.put_facts(facts)
    retract_removed(lstore, facts, bag.failed_files)
    p.write_text("version: 1\nlineage:\n" + other, encoding="utf-8")  # the first line is removed
    facts, *_, bag = load_overlays([p], lstore, today=TODAY)
    assert retract_removed(lstore, facts, bag.failed_files) == 0
    lstore.put_facts(facts)
    assert _edges(lstore) == ["shop.orders", "shop.refunds"]
    assert _facts(lstore)[0].source == f"overlay:{p}:3"


def test_equal_inputs_different_transform_is_reported_not_ordered(
    lstore: SQLiteStore, tmp_path: Path
) -> None:
    def body(first: str, second: str) -> str:
        c = "  - column: analytics.revenue_daily.net_revenue\n    inputs: [shop.orders.amount]\n"
        return f"{c}    transform: {first}\n{c}    transform: {second}\n"

    for first, second in (("copy", "cast"), ("cast", "copy")):
        facts, *_, bag = load_overlays([_write(tmp_path, body(first, second))], lstore, today=TODAY)
        assert facts == []  # neither wins by line order
        d = list(bag)
        assert [x.code for x in d] == ["SG208"]
        assert "transform" in d[0].message and "line 3" in d[0].message and "line 6" in d[0].message


@pytest.mark.skipif(not lineage_available(), reason="needs sqlglot")
def test_ignore_after_view_redefinition(lstore: SQLiteStore, tmp_path: Path) -> None:
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    v = ObjectRef(None, "shop", "v_orders")
    reader = ViewFakeReader(
        {"shop.orders": ["id", "amount"], "shop.customers": ["id", "email"],
         "shop.v_orders": ["id", "amount"]},
        {"shop.v_orders": "CREATE VIEW shop.v_orders AS SELECT id, amount FROM shop.orders"},
    )  # fmt: skip
    st = lstore
    crawl(reader, st, now=lambda: now)
    assert _edges(st, v) == ["shop.orders"]
    p = _write(
        tmp_path,
        "  - table: shop.v_orders\n    upstream: [shop.orders]\n    ignore: true\n",
    )
    facts, *_ = load_overlays([p], st, today=TODAY)
    st.put_facts(facts)
    assert _edges(st, v) == []
    assert lineage_ignore_diagnostics(st, facts) == []  # it matches the old link: no SG213
    reader.views["shop.v_orders"] = (
        "CREATE VIEW shop.v_orders AS SELECT id, email AS amount FROM shop.customers"
    )
    crawl(reader, st, now=lambda: now + timedelta(days=1))
    facts, *_ = load_overlays([p], st, today=TODAY)
    st.put_facts(facts)
    assert [d.code for d in lineage_ignore_diagnostics(st, facts)] == ["SG213"]
    assert _edges(st, v) == ["shop.customers"]  # the new link shows; nothing old came back
    kept = {f.value.get("upstream", [None])[0] for f in _facts(st, v) if f.source == VIEW_SOURCE}
    assert kept == {"shop.customers"}


def test_guru_load_overlays_reports_sg213(lstore: SQLiteStore, tmp_path: Path) -> None:
    from schemaguru import Guru

    lstore.put_facts([_parsed(LEGACY, ["shop.customers"])])
    p = _write(
        tmp_path,
        "  - table: analytics.legacy_view\n    upstream: [shop.orders]\n    ignore: true\n",
    )
    g = Guru(store=lstore)
    assert [d.code for d in g.load_overlays(p)] == ["SG213"]


ENTRY = "  - table: analytics.revenue_daily\n    upstream: [shop.orders]\n"
COL = "  - column: analytics.revenue_daily.net_revenue\n    inputs: [shop.orders.amount]\n"


@pytest.mark.parametrize("first_order", ["ba", "ab"])
@pytest.mark.parametrize("second_order", ["ab", "ba"])
def test_fact_declared_in_two_files_survives_one_dropping_and_one_failing(
    lstore: SQLiteStore, tmp_path: Path, first_order: str, second_order: str
) -> None:
    a = _write(tmp_path, ENTRY, "a.yaml")
    b = _write(tmp_path, ENTRY, "b.yaml")
    paths = {"a": a, "b": b}
    facts, *_, bag = load_overlays([paths[c] for c in first_order], lstore, today=TODAY)
    lstore.put_facts(facts)
    retract_removed(lstore, facts, bag.failed_files)
    a.write_text("version: 1\nlineage:\n", encoding="utf-8")  # A drops the entry
    b.write_text("version: [unclosed\n", encoding="utf-8")  # B is invalid YAML
    facts, *_, bag = load_overlays([paths[c] for c in second_order], lstore, today=TODAY)
    assert retract_removed(lstore, facts, bag.failed_files) == 0  # B still declares it
    assert _edges(lstore) == ["shop.orders"]
    b.write_text("version: 1\nlineage:\n", encoding="utf-8")  # B recovers, without the entry
    facts, *_, bag = load_overlays([paths[c] for c in second_order], lstore, today=TODAY)
    assert retract_removed(lstore, facts, bag.failed_files) == 1
    assert _edges(lstore) == []


def test_old_scalar_state_format_is_read(lstore: SQLiteStore, tmp_path: Path) -> None:
    p = _write(tmp_path, ENTRY)
    facts, *_ = load_overlays([p], lstore, today=TODAY)
    lstore.put_facts(facts)
    lstore.crawl_state_set("overlay_facts", {"facts": {facts[0].id: str(p)}})
    p.write_text("version: 1\nlineage:\n", encoding="utf-8")
    facts, *_, bag = load_overlays([p], lstore, today=TODAY)
    assert retract_removed(lstore, facts, bag.failed_files) == 1


def test_cross_file_transform_collision_is_reported_and_dropped(
    lstore: SQLiteStore, tmp_path: Path
) -> None:
    a = _write(tmp_path, COL + "    transform: copy\n", "a.yaml")
    b = _write(tmp_path, COL + "    transform: cast\n", "b.yaml")
    for order in ([a, b], [b, a]):
        facts, *_, bag = load_overlays(order, lstore, today=TODAY)
        assert facts == []
        d = list(bag)
        assert [x.code for x in d] == ["SG208"]
        assert f"{a}:3" in d[0].message and f"{b}:3" in d[0].message


def test_cross_file_identical_duplicates_are_silent(lstore: SQLiteStore, tmp_path: Path) -> None:
    a = _write(tmp_path, COL + "    transform: copy\n", "a.yaml")
    b = _write(tmp_path, COL + "    transform: copy\n", "b.yaml")
    facts, *_, bag = load_overlays([a, b], lstore, today=TODAY)
    assert list(bag) == [] and len(facts) == 2 and len({f.id for f in facts}) == 1


def test_ignore_with_transform_is_sg208(lstore: SQLiteStore, tmp_path: Path) -> None:
    p = _write(tmp_path, COL + "    transform: copy\n    ignore: true\n")
    facts, *_, bag = load_overlays([p], lstore, today=TODAY)
    assert facts == []
    d = list(bag)
    assert [x.code for x in d] == ["SG208"]
    assert "`transform` is meaningless on an ignore entry" in d[0].message
    ok = _write(tmp_path, COL + "    ignore: true\n", "ok.yaml")
    assert len(load_overlays([ok], lstore, today=TODAY)[0]) == 1
