from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from schemaguru.diagnostics import DiagnosticBag
from schemaguru.lineage.parse import lineage_available
from schemaguru.lineage.worker import ParserPool
from schemaguru.model.facts import Fact, Provenance, Status
from schemaguru.model.refs import ObjectRef
from schemaguru.model.structure import Column, Table
from schemaguru.overlays import apply_dbt, import_dbt, retract_dbt_lineage
from schemaguru.store.sqlite import SQLiteStore

from .conftest import FIXTURES

DBT = FIXTURES / "dbt"
pytestmark = pytest.mark.skipif(not lineage_available(), reason="needs sqlglot")


def _add(store: SQLiteStore, table: str, cols: list[str]) -> None:
    ref = ObjectRef(None, "shop", table)
    store.upsert_tables([Table(ref, "table")])
    store.upsert_columns(
        ref,
        [
            Column(ObjectRef(None, "shop", table, c), i, "STRING", "STRING", True, None, None)
            for i, c in enumerate(cols, 1)
        ],
    )


@pytest.fixture
def lstore(store: SQLiteStore) -> SQLiteStore:
    _add(store, "refunds", ["order_id", "refund_amount"])
    _add(store, "revenue", ["customer_id", "net_revenue"])
    return store


@pytest.fixture(scope="module")
def pool() -> Iterator[ParserPool]:
    with ParserPool() as p:
        yield p


def _lineage(facts: list[Fact], kind: str) -> dict[str, Fact]:
    return {f.subject.canonical(): f for f in facts if f.kind.name == kind}  # type: ignore[union-attr]


def _codes(bag: DiagnosticBag) -> list[str]:
    return [d.code for d in bag]


def test_table_lineage_from_depends_on(lstore: SQLiteStore, pool: ParserPool) -> None:
    facts = import_dbt(DBT / "manifest_lineage.json", lstore, parser_pool=pool)
    table = _lineage(facts, "LINEAGE")
    orders = table["shop.orders"]
    assert orders.provenance is Provenance.DECLARED and orders.status is Status.UNVERIFIED
    assert orders.source == "dbt:manifest:model.shop.orders"
    assert orders.value["upstream"] == [
        "shop.customers", "shop.events", "shop.payments", "shop.sessions",
    ]  # fmt: skip
    assert table["shop.revenue"].value["upstream"] == ["shop.orders", "shop.refunds"]
    assert "shop.customers" not in table  # no inputs, no fact


def test_ephemeral_inputs_inherited(lstore: SQLiteStore, pool: ParserPool) -> None:
    facts = import_dbt(DBT / "manifest_lineage.json", lstore, parser_pool=pool)
    up = _lineage(facts, "LINEAGE")["shop.orders"].value["upstream"]
    # eph -> sessions + eph2 -> events (a cycle eph <-> eph2 ends); eph itself is no relation
    assert "shop.sessions" in up and "shop.events" in up
    assert not any("eph" in u for u in up)
    assert "shop.eph" not in _lineage(facts, "LINEAGE")


def test_tests_macros_metrics_not_relations(lstore: SQLiteStore, pool: ParserPool) -> None:
    facts = import_dbt(DBT / "manifest_lineage.json", lstore, parser_pool=pool)
    orders = _lineage(facts, "LINEAGE")["shop.orders"]
    flat = orders.value["upstream"] + orders.value["unresolved"]
    assert not any("t1" in n or "m1" in n or "macro" in n for n in flat)


def test_unresolved_relation_named(lstore: SQLiteStore, pool: ParserPool) -> None:
    facts = import_dbt(DBT / "manifest_lineage.json", lstore, parser_pool=pool)
    assert _lineage(facts, "LINEAGE")["shop.orders"].value["unresolved"] == ["analytics.shop.ghost"]


def test_column_lineage_from_compiled_code(lstore: SQLiteStore, pool: ParserPool) -> None:
    bag = DiagnosticBag()
    facts = import_dbt(DBT / "manifest_lineage.json", lstore, diagnostics=bag, parser_pool=pool)
    cols = _lineage(facts, "COLUMN_LINEAGE")
    net = cols["shop.revenue.net_revenue"]
    assert net.value["inputs"] == ["shop.orders.amount", "shop.refunds.refund_amount"]
    assert net.value["transform"] == "aggregate"
    assert net.provenance is Provenance.DECLARED and net.status is Status.UNVERIFIED
    assert net.source == "dbt:manifest:model.shop.revenue"
    assert cols["shop.revenue.customer_id"].value["inputs"] == ["shop.orders.customer_id"]
    assert _codes(bag) == []


def _with_adapter(tmp_path: Path, adapter: str, sql: str) -> Path:
    m = json.loads((DBT / "manifest_lineage.json").read_text("utf-8"))
    m["metadata"]["adapter_type"] = adapter
    m["nodes"]["model.shop.revenue"]["compiled_code"] = sql
    p = tmp_path / f"m_{adapter}.json"
    p.write_text(json.dumps(m), encoding="utf-8")
    return p


def test_compiled_code_is_never_stored(
    lstore: SQLiteStore, pool: ParserPool, tmp_path: Path
) -> None:
    sql = (
        'select o.customer_id, sum(o.amount) as net_revenue from "analytics"."shop"."orders" '
        "as o where o.status = 'hunter2' and o.amount > 987654 group by o.customer_id"
    )
    path = _with_adapter(tmp_path, "postgres", sql)
    facts = import_dbt(path, lstore, parser_pool=pool)
    blob = json.dumps([f.to_dict() for f in facts])
    assert "hunter2" not in blob and "987654" not in blob
    rev = _lineage(facts, "LINEAGE")["shop.revenue"]
    assert rev.evidence is not None and rev.evidence.sql  # the literal-free template is kept


def test_adapter_alias_sqlserver_is_tsql(
    lstore: SQLiteStore, pool: ParserPool, tmp_path: Path
) -> None:
    sql = (
        "select o.customer_id, sum(o.amount) - sum(r.refund_amount) as net_revenue "
        "from [analytics].[shop].[orders] as o join [analytics].[shop].[refunds] as r "
        "on o.order_id = r.order_id group by o.customer_id"
    )  # bracket quoting only parses as T-SQL
    bag = DiagnosticBag()
    facts = import_dbt(
        _with_adapter(tmp_path, "sqlserver", sql), lstore, diagnostics=bag, parser_pool=pool
    )
    net = _lineage(facts, "COLUMN_LINEAGE")["shop.revenue.net_revenue"]
    assert net.value["inputs"] == ["shop.orders.amount", "shop.refunds.refund_amount"]
    assert "SG211" not in _codes(bag)


def test_adapter_spark_not_unsupported(
    lstore: SQLiteStore, pool: ParserPool, tmp_path: Path
) -> None:
    sql = (
        "select o.customer_id, sum(o.amount) as net_revenue from `shop`.`orders` as o "
        "group by o.customer_id"
    )
    bag = DiagnosticBag()
    facts = import_dbt(
        _with_adapter(tmp_path, "spark", sql), lstore, diagnostics=bag, parser_pool=pool
    )
    assert "SG211" not in _codes(bag), [d.message for d in bag]
    assert _lineage(facts, "COLUMN_LINEAGE")["shop.revenue.net_revenue"].value["inputs"] == [
        "shop.orders.amount"
    ]


def test_uncompiled_manifest_one_sg214_table_level_only(
    lstore: SQLiteStore, pool: ParserPool
) -> None:
    bag = DiagnosticBag()
    facts = import_dbt(DBT / "manifest_uncompiled.json", lstore, diagnostics=bag, parser_pool=pool)
    assert _codes(bag) == ["SG214"]
    assert _lineage(facts, "LINEAGE") and not _lineage(facts, "COLUMN_LINEAGE")


def test_no_pool_given_still_parses(lstore: SQLiteStore) -> None:
    facts = import_dbt(DBT / "manifest_lineage.json", lstore)
    assert _lineage(facts, "COLUMN_LINEAGE")


def test_unparseable_compiled_code_is_sg211_without_sql(
    lstore: SQLiteStore, pool: ParserPool, tmp_path: Path
) -> None:
    secret = "select ((( 'hunter2' from"
    bag = DiagnosticBag()
    facts = import_dbt(
        _with_adapter(tmp_path, "postgres", secret), lstore, diagnostics=bag, parser_pool=pool
    )
    assert _codes(bag) == ["SG211"]
    assert "hunter2" not in "".join(d.message for d in bag)
    assert not _lineage(facts, "COLUMN_LINEAGE") and "shop.revenue" in _lineage(facts, "LINEAGE")


def _ids(store: SQLiteStore, kinds: tuple[str, ...] = ("LINEAGE", "COLUMN_LINEAGE")) -> set[str]:
    return {f.id for f in store.facts_for(None, kinds)}


def test_reimport_retracts_removed_lineage(lstore: SQLiteStore, pool: ParserPool) -> None:
    apply_dbt(FIXTURES / "dbt" / "manifest.json", lstore, parser_pool=pool)
    desc_before = _ids(lstore, ("DESCRIPTION", "JOIN", "VALUES", "PRIMARY_KEY"))
    assert desc_before
    first = apply_dbt(DBT / "manifest_lineage.json", lstore, parser_pool=pool)
    ids1 = {f.id for f in first if f.kind.name in ("LINEAGE", "COLUMN_LINEAGE")}
    assert ids1 and _ids(lstore) == ids1
    second = apply_dbt(DBT / "manifest_lineage_v2.json", lstore, parser_pool=pool)
    ids2 = {f.id for f in second if f.kind.name in ("LINEAGE", "COLUMN_LINEAGE")}
    assert _ids(lstore) == ids2 and ids1 != ids2
    assert not _lineage(list(lstore.facts_for(None, ("COLUMN_LINEAGE",))), "COLUMN_LINEAGE")
    orders = _lineage(list(lstore.facts_for(None, ("LINEAGE",))), "LINEAGE")["shop.orders"]
    assert "shop.customers" not in orders.value["upstream"]
    assert _ids(lstore, ("DESCRIPTION", "JOIN", "VALUES", "PRIMARY_KEY")) == desc_before


def test_retraction_leaves_other_lineage_alone(lstore: SQLiteStore, pool: ParserPool) -> None:
    other = Fact.make(
        "LINEAGE",
        ObjectRef(None, "shop", "payments"),
        {"upstream": ["shop.orders"], "unresolved": [], "ignore": False},
        Provenance.HUMAN,
        Status.UNVERIFIED,
        source="overlay:x.yaml:1",
    )
    lstore.put_facts([other])
    apply_dbt(DBT / "manifest_lineage.json", lstore, parser_pool=pool)
    assert retract_dbt_lineage(lstore, []) > 0
    assert other.id in _ids(lstore) and len(_ids(lstore)) == 1


def test_bad_manifest_retracts_nothing(
    lstore: SQLiteStore, pool: ParserPool, tmp_path: Path
) -> None:
    apply_dbt(DBT / "manifest_lineage.json", lstore, parser_pool=pool)
    before = _ids(lstore)
    bad = tmp_path / "bad.json"
    bad.write_text("[]", encoding="utf-8")
    bag = DiagnosticBag()
    assert apply_dbt(bad, lstore, diagnostics=bag, parser_pool=pool) == []
    assert apply_dbt(tmp_path / "none.json", lstore, diagnostics=bag, parser_pool=pool) == []
    assert _codes(bag) == ["SG209", "SG209"]
    assert _ids(lstore) == before


def test_same_relation_different_transform_is_reported_not_order_dependent(
    lstore: SQLiteStore, pool: ParserPool, tmp_path: Path
) -> None:
    m = json.loads((DBT / "manifest_lineage.json").read_text("utf-8"))
    base = m["nodes"].pop("model.shop.revenue")
    base["alias"] = "revenue"  # two nodes, one relation
    twin = copy.deepcopy(base)
    twin["compiled_code"] = (
        "select o.customer_id, cast(o.amount as int) + 0 * r.refund_amount as net_revenue "
        'from "analytics"."shop"."orders" as o join "analytics"."shop"."refunds" as r '
        "on o.order_id = r.order_id"
    )
    outcomes = []
    for order in (("a_rev", "z_rev"), ("z_rev", "a_rev")):  # insertion order differs
        m2 = copy.deepcopy(m)
        for n in order:
            m2["nodes"][f"model.shop.{n}"] = copy.deepcopy(base if n == "a_rev" else twin)
        p = tmp_path / f"{order[0]}.json"
        p.write_text(json.dumps(m2), encoding="utf-8")
        bag = DiagnosticBag()
        facts = import_dbt(p, lstore, diagnostics=bag, parser_pool=pool)
        rev = [f for f in facts if f.subject.canonical().startswith("shop.revenue")]  # type: ignore[union-attr]
        outcomes.append((sorted((f.id, f.source) for f in rev), _codes(bag)))
    assert outcomes[0] == outcomes[1]
    assert outcomes[0][1] == ["SG215"]  # the transform differs on net_revenue only
    assert {s for _, s in outcomes[0][0]} == {"dbt:manifest:model.shop.a_rev"}
    net = next(f for f in facts if f.subject.canonical() == "shop.revenue.net_revenue")
    assert net.value["transform"] == "aggregate"


def test_never_raises_on_hostile_lineage_fields(lstore: SQLiteStore, tmp_path: Path) -> None:
    m: dict[str, Any] = json.loads((DBT / "manifest_lineage.json").read_text("utf-8"))
    for n in m["nodes"].values():
        n["compiled_code"] = 7
        n["depends_on"] = {"nodes": [None, 3, {"a": 1}]}
        n["config"] = "x"
    m["metadata"] = []
    p = tmp_path / "h.json"
    p.write_text(json.dumps(m), encoding="utf-8")
    import_dbt(p, lstore, diagnostics=DiagnosticBag())


# ----- without the extra: earlier column lineage is kept only while its definition is unchanged --


def _columns(store: SQLiteStore) -> set[str]:
    return _ids(store, ("COLUMN_LINEAGE",))


def _reimport_without_sqlglot(
    store: SQLiteStore, path: Path, monkeypatch: pytest.MonkeyPatch
) -> DiagnosticBag:
    import sys

    monkeypatch.setitem(sys.modules, "sqlglot", None)
    bag = DiagnosticBag()
    apply_dbt(path, store, diagnostics=bag)
    return bag


def test_no_extra_keeps_unchanged_column_lineage(
    lstore: SQLiteStore, pool: ParserPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    apply_dbt(DBT / "manifest_lineage.json", lstore, parser_pool=pool)
    cols = _columns(lstore)
    assert cols
    bag = _reimport_without_sqlglot(lstore, DBT / "manifest_lineage.json", monkeypatch)
    assert _columns(lstore) == cols and "SG212" in _codes(bag)


def test_no_extra_retracts_column_lineage_of_a_removed_model(
    lstore: SQLiteStore, pool: ParserPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    apply_dbt(DBT / "manifest_lineage.json", lstore, parser_pool=pool)
    assert _columns(lstore)
    _reimport_without_sqlglot(lstore, DBT / "manifest_lineage_v2.json", monkeypatch)
    assert _columns(lstore) == set()


def test_no_extra_retracts_column_lineage_of_a_changed_definition(
    lstore: SQLiteStore, pool: ParserPool, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    apply_dbt(DBT / "manifest_lineage.json", lstore, parser_pool=pool)
    assert _columns(lstore)
    m = json.loads((DBT / "manifest_lineage.json").read_text("utf-8"))
    m["nodes"]["model.shop.revenue"]["compiled_code"] += " -- changed"
    p = tmp_path / "changed.json"
    p.write_text(json.dumps(m), encoding="utf-8")
    _reimport_without_sqlglot(lstore, p, monkeypatch)
    assert _columns(lstore) == set()
    assert "shop.revenue" in _lineage(list(lstore.facts_for(None, ("LINEAGE",))), "LINEAGE")


# ----- a name that matches two tables is never guessed ------------------------------------------


def test_ambiguous_bare_name_is_not_resolved_to_either_table(
    lstore: SQLiteStore, pool: ParserPool, tmp_path: Path
) -> None:
    for schema in ("a", "b"):
        ref = ObjectRef(None, schema, "items")
        lstore.upsert_tables([Table(ref, "table")])
        lstore.upsert_columns(
            ref,
            [Column(ObjectRef(None, schema, "items", "qty"), 1, "INT", "INT", True, None, None)],
        )
    m = json.loads((DBT / "manifest_lineage.json").read_text("utf-8"))
    for schema in ("a", "b"):
        m["nodes"][f"model.shop.items_{schema}"] = {
            "resource_type": "model", "name": "items", "database": "analytics", "schema": schema,
            "config": {}, "depends_on": {"nodes": []},
        }  # fmt: skip
    m["nodes"]["model.shop.revenue"]["compiled_code"] = (
        "select i.qty as net_revenue from items as i"
    )
    p = tmp_path / "amb.json"
    p.write_text(json.dumps(m), encoding="utf-8")
    facts = import_dbt(p, lstore, parser_pool=pool)
    target = [
        f
        for f in facts
        if f.kind.name == "COLUMN_LINEAGE" and f.subject.canonical() == "shop.revenue.net_revenue"
    ]
    assert target, "the target column's COLUMN_LINEAGE fact must exist (else this is vacuous)"
    for f in target:
        assert not any(i.endswith("items.qty") for i in f.value["inputs"])


# ----- a failing lineage step leaves the 0.1.0 facts to import_dbt, nothing to apply_dbt --------


def test_import_dbt_keeps_base_facts_when_lineage_raises(
    lstore: SQLiteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from schemaguru.overlays import dbt as mod

    def boom(self: Any, pool: Any) -> None:
        raise RuntimeError("select 'hunter2'")

    monkeypatch.setattr(mod._Importer, "lineage", boom)
    bag = DiagnosticBag()
    facts = import_dbt(FIXTURES / "dbt" / "manifest.json", lstore, diagnostics=bag)
    assert {f.kind.name for f in facts} >= {"DESCRIPTION", "JOIN", "VALUES"}
    assert [d.code for d in bag] == ["SG209"] and "hunter2" not in bag_text(bag)
    assert apply_dbt(FIXTURES / "dbt" / "manifest.json", lstore, diagnostics=DiagnosticBag()) == []
    assert not lstore.facts_for(None, ("DESCRIPTION",))


def bag_text(bag: DiagnosticBag) -> str:
    return " ".join(d.message for d in bag)


@pytest.mark.parametrize("adapter", ["bigquery", "databricks", "spark", "mysql", "mariadb"])
def test_compiled_code_double_quoted_strings_are_blanked(
    lstore: SQLiteStore, pool: ParserPool, tmp_path: Path, adapter: str
) -> None:
    from schemaguru.store.export import export_json

    sql = (
        "select o.customer_id, sum(o.amount) as net_revenue from `shop`.`orders` as o "
        'where o.note = "Maria Garcia" or o.note = "12 Baker Street" group by o.customer_id'
    )
    facts = apply_dbt(_with_adapter(tmp_path, adapter, sql), lstore, parser_pool=pool)
    rev = _lineage(facts, "LINEAGE")["shop.revenue"]
    assert rev.evidence is not None and rev.evidence.sql, "the template is kept"
    exported = tmp_path / "export.json"
    export_json(lstore, exported)
    for blob in (json.dumps([f.to_dict() for f in facts]), exported.read_text("utf-8")):
        assert "Maria Garcia" not in blob and "12 Baker Street" not in blob, adapter
