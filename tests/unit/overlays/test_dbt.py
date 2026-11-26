from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from schemaguru.diagnostics import DiagnosticBag
from schemaguru.model.facts import Provenance, Status
from schemaguru.model.joins import JoinRef
from schemaguru.model.refs import ObjectRef
from schemaguru.overlays import import_dbt
from schemaguru.store.sqlite import SQLiteStore

from .conftest import FIXTURES

MANIFEST = FIXTURES / "dbt" / "manifest.json"


def test_dbt_relationships_become_declared_joins(store: SQLiteStore) -> None:
    facts = import_dbt(MANIFEST, store)
    assert all(f.provenance is Provenance.DECLARED and f.status is Status.UNVERIFIED for f in facts)
    joins = [f for f in facts if f.kind.name == "JOIN"]
    assert len(joins) == 1 and isinstance(joins[0].subject, JoinRef)
    assert joins[0].subject.canonical() == "shop.orders.customer_id -> shop.customers.customer_id"
    assert joins[0].source == "dbt:manifest:test.shop.rel"


def test_dbt_descriptions_and_expectations(store: SQLiteStore) -> None:
    facts = import_dbt(MANIFEST, store)
    desc = {f.subject.canonical(): f.value["text"] for f in facts if f.kind.name == "DESCRIPTION"}  # type: ignore[union-attr]
    assert desc == {"shop.orders": "One row per order.", "shop.orders.status": "Order state."}
    vals = next(f for f in facts if f.kind.name == "VALUES")
    assert vals.subject == ObjectRef(None, "shop", "orders", "status")
    assert vals.value["values"] == ["paid", "refunded"]
    pk = next(f for f in facts if f.kind.name == "PRIMARY_KEY")
    assert pk.value["columns"] == ["order_id"]
    assert not [f for f in facts if f.kind.name == "NULL_RATE"]  # not_null is not imported


def test_dbt_relationship_cardinality_is_unknown(store: SQLiteStore) -> None:
    join = next(f for f in import_dbt(MANIFEST, store) if f.kind.name == "JOIN")
    assert join.value["cardinality"] is None


def _manifest(tmp_path: Path, tests: dict[str, Any]) -> Path:
    base = json.loads(MANIFEST.read_text(encoding="utf-8"))
    base["nodes"] = {k: v for k, v in base["nodes"].items() if not k.startswith("test.")}
    for name, (tname, col) in tests.items():
        base["nodes"][f"test.shop.{name}"] = {
            "resource_type": "test", "attached_node": "model.shop.orders",
            "test_metadata": {"name": tname, "kwargs": {"column_name": col}},
        }  # fmt: skip
    p = tmp_path / "m.json"
    p.write_text(json.dumps(base), encoding="utf-8")
    return p


def _pks(store: SQLiteStore, path: Path) -> list[Any]:
    return [f.value["columns"] for f in import_dbt(path, store) if f.kind.name == "PRIMARY_KEY"]


def test_dbt_unique_is_pk_only_with_not_null_and_single(store: SQLiteStore, tmp_path: Path) -> None:
    both = _manifest(tmp_path, {"u": ("unique", "order_id"), "n": ("not_null", "order_id")})
    assert _pks(store, both) == [["order_id"]]
    assert _pks(store, _manifest(tmp_path, {"u": ("unique", "order_id")})) == []
    assert _pks(store, _manifest(tmp_path, {"n": ("not_null", "order_id")})) == []
    two = _manifest(
        tmp_path,
        {"u1": ("unique", "order_id"), "u2": ("unique", "customer_id"),
         "n": ("not_null", "order_id")},
    )  # fmt: skip
    assert _pks(store, two) == []


@pytest.mark.parametrize(
    "manifest",
    [
        {"nodes": []},
        {"nodes": {"t": {"resource_type": "test", "attached_node": "m", "test_metadata": {
            "name": "unique", "kwargs": []}}}},
        {"nodes": {"t": {"resource_type": "test", "attached_node": "model.shop.orders",
                         "depends_on": [], "test_metadata": {
            "name": "relationships", "kwargs": {"column_name": "customer_id", "to": 5}}}}},
        {"nodes": {"m": {"resource_type": "model", "name": "orders", "schema": "shop",
                         "columns": []}}},
        {"nodes": {"m": {"resource_type": "model", "name": 5, "schema": ["x"]}}, "sources": 3},
        {"nodes": {"t": {"resource_type": "test", "test_metadata": []}}},
        [],
    ],
)  # fmt: skip
def test_dbt_malformed_manifest_never_raises(
    store: SQLiteStore, tmp_path: Path, manifest: Any
) -> None:
    p = tmp_path / "m.json"
    p.write_text(json.dumps(manifest), encoding="utf-8")
    import_dbt(p, store, diagnostics=DiagnosticBag())


def test_dbt_unreadable_manifest(store: SQLiteStore, tmp_path: Path) -> None:
    bag = DiagnosticBag()
    bad = tmp_path / "m.json"
    bad.write_text("{nope", encoding="utf-8")
    assert import_dbt(bad, store, diagnostics=bag) == []
    assert import_dbt(tmp_path / "none.json", store, diagnostics=bag) == []
    assert [d.code for d in bag] == ["SG209", "SG209"]


def test_unique_count_includes_unresolvable(store: SQLiteStore, tmp_path: Path) -> None:
    m = _manifest(
        tmp_path,
        {"u1": ("unique", "order_id"), "u2": ("unique", "no_such_column"),
         "n": ("not_null", "order_id")},
    )  # fmt: skip
    assert _pks(store, m) == []  # a second unique test exists, even if it cannot be mapped


def test_module_docstring() -> None:
    import schemaguru.overlays.dbt as mod

    doc = mod.__doc__ or ""
    assert "DECLARED" in doc and "UNVERIFIED" in doc and "manifest" in doc
