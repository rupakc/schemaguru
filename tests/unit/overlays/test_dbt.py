from __future__ import annotations

from pathlib import Path

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
    nn = next(f for f in facts if f.kind.name == "NULL_RATE")
    assert nn.value["rate"] == 0.0


def test_dbt_unreadable_manifest(store: SQLiteStore, tmp_path: Path) -> None:
    bag = DiagnosticBag()
    bad = tmp_path / "m.json"
    bad.write_text("{nope")
    assert import_dbt(bad, store, diagnostics=bag) == []
    assert import_dbt(tmp_path / "none.json", store, diagnostics=bag) == []
    assert [d.code for d in bag] == ["SG209", "SG209"]
