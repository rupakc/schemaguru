"""Runs in the ``no-extras`` job (stdlib, pytest and schemaguru only) and in ``[dev]`` jobs too:
``sqlglot`` is made unimportable here, so the same assertions hold in both."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from schemaguru.diagnostics import DiagnosticBag
from schemaguru.model.refs import ObjectRef
from schemaguru.model.structure import Column, Table
from schemaguru.overlays import apply_dbt
from schemaguru.store.sqlite import SQLiteStore

MANIFEST = Path(__file__).resolve().parents[1] / "fixtures" / "dbt" / "manifest_lineage.json"
TABLES = {
    "orders": ["order_id", "customer_id", "amount"],
    "customers": ["customer_id"],
    "payments": ["payment_id"],
    "events": ["event_id"],
    "sessions": ["session_id"],
    "refunds": ["order_id", "refund_amount"],
    "revenue": ["customer_id", "net_revenue"],
}


def test_table_level_without_sqlglot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "sqlglot", None)
    store = SQLiteStore(tmp_path / "t.db")
    try:
        for t, cols in TABLES.items():
            ref = ObjectRef(None, "shop", t)
            store.upsert_tables([Table(ref, "table")])
            store.upsert_columns(
                ref,
                [
                    Column(ObjectRef(None, "shop", t, c), i, "STRING", "STRING", True, None, None)
                    for i, c in enumerate(cols, 1)
                ],
            )
        bag = DiagnosticBag()
        facts = apply_dbt(MANIFEST, store, diagnostics=bag)
        kinds = {f.kind.name for f in facts}
        assert "LINEAGE" in kinds and "COLUMN_LINEAGE" not in kinds
        assert [d.code for d in bag] == ["SG212"]
    finally:
        store.close()
