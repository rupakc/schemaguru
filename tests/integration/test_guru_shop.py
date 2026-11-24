"""End to end on the shop warehouse: crawl -> measure -> overlay -> context (spec 12, 13, 14).

Local DuckDB only (no service container), so it runs with the unit suite. Measurement goes
through the test ``FakeBackend`` until FactLang ships ``Checker.measure``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import schemaguru as sg
from schemaguru.catalog import DBAPIExecutor, reader_for
from tests.fixtures.shop import ShopFacts
from tests.unit.measure.fake_backend import FakeBackend

OVERLAY = Path(__file__).resolve().parent.parent / "fixtures" / "overlays" / "spec_example.yaml"


def test_guru_shop_end_to_end(tmp_path: Path, shop_duckdb: tuple[Any, ShopFacts]) -> None:
    conn, facts = shop_duckdb
    guru = sg.Guru(
        reader=reader_for("duckdb", DBAPIExecutor(conn)),
        backend=FakeBackend(conn),
        store=str(tmp_path / "shop.db"),
        dialect="duckdb",
    )
    crawled = guru.crawl()
    assert crawled.tables_seen >= 9 and not crawled.errors

    measured = guru.measure(all=True)
    assert not [d for d in measured.errors if d.code != "SG303"]
    assert {"shop.orders", "shop.payments", "shop.customers"} <= {
        r.canonical() for r in measured.measured_tables
    }

    diags = guru.load_overlays(OVERLAY)  # the spec 14.1 example, verbatim
    # its never_query table does not exist in the shop: reported, not raised
    assert any(d.code == "SG203" and "orders_backup_2019" in d.message for d in diags)

    view = guru.context("refund rate by month")
    text = view.text
    start = text.index("[shop.orders]")
    end = text.find("\n\n[", start + 1)
    orders = text[start : end if end != -1 else len(text)]
    # case drift: the human's declared values vs the observed upper-case rows
    assert "case drift: 'paid'/'PAID' differ only in case" in orders
    assert f"observed also 'PAID' ({facts.status_counts['PAID']:,} rows)" in orders
    # the overlay's waiver applies to the contradiction, and says so
    assert "WAIVED until 2027-01-31 (legacy upper-case rows being migrated)" in orders
    # soft delete: the human predicate, next to the measured TRUE/NULL shape of its marker
    assert "live rows: (deleted IS NULL OR NOT deleted) (human, unchecked)" in orders
    deleted = next(ln for ln in orders.splitlines() if ln.strip().startswith("deleted "))
    assert f"values: TRUE ({facts.deleted_true})" in deleted
    null_pct = round(100 * facts.deleted_null / (facts.deleted_null + facts.deleted_true))
    assert f"null {null_pct}%" in deleted and "FALSE" not in deleted
    # the thin-but-correct payments join, with its coverage
    assert "← shop.payments.order_id (coverage 5%)" in orders
    assert view.tokens <= 4000
    assert "unmeasured" not in orders

    guru.close()
