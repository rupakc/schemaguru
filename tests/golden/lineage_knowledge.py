"""The shop store plus lineage: a dbt model, a view and one overlay link (0.2.0 spec 4.6).

``analytics.revenue_daily`` carries DECLARED lineage (dbt), ``shop.v_paid_orders`` CATALOG lineage
(view definition) and ``shop.order_summary`` a HUMAN overlay link that replaces what was parsed.
Facts are unverified whatever their provenance.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from schemaguru.model.facts import Fact, Provenance, Status
from schemaguru.model.kinds import FactKind
from schemaguru.model.lineage import (
    DBT_SOURCE,
    VIEW_SOURCE,
    column_lineage_value,
    table_lineage_value,
)
from schemaguru.model.refs import ObjectRef
from schemaguru.model.structure import Column, Table
from schemaguru.store.sqlite import SQLiteStore
from tests.golden.shop_knowledge import c, shop_facts, shop_store, t

REVENUE = ObjectRef(None, "analytics", "revenue_daily")
REVENUE_COL = ObjectRef(None, "analytics", "revenue_daily", "revenue")
PAID = t("v_paid_orders")
SUMMARY = t("order_summary")
LINEAGE_TABLES = (REVENUE, PAID, SUMMARY)


def _lineage(
    subject: ObjectRef, upstream: list[str], prov: Provenance, source: str, **kw: object
) -> Fact:
    return Fact.make(
        FactKind.LINEAGE,
        subject,
        table_lineage_value(upstream, kw.get("unresolved", ())),  # type: ignore[arg-type]
        prov,
        Status.UNVERIFIED,
        source=source,
    )


def _column(
    subject: ObjectRef, inputs: list[str], transform: str, prov: Provenance, source: str
) -> Fact:
    return Fact.make(
        FactKind.COLUMN_LINEAGE,
        subject,
        column_lineage_value(inputs, transform),
        prov,
        Status.UNVERIFIED,
        source=source,
    )


def lineage_facts() -> list[Fact]:
    dbt = DBT_SOURCE + ":model.shop.revenue_daily"
    D, C, H = Provenance.DECLARED, Provenance.CATALOG, Provenance.HUMAN
    orders_payments = ["shop.orders", "shop.payments"]
    return [
        _lineage(REVENUE, [*orders_payments, "shop.refunds", "shop.order_items"], D, dbt),
        _column(REVENUE_COL, ["shop.orders.amount", "shop.refunds.amount"], "aggregate", D, dbt),
        _lineage(PAID, orders_payments, C, VIEW_SOURCE, unresolved=["ext.raw_payments"]),
        # same inputs, no unresolved name: a second line (merging needs equal shapes)
        _lineage(PAID, orders_payments, D, dbt),
        _column(c("v_paid_orders", "amount"), ["shop.payments.amount"], "copy", C, VIEW_SOURCE),
        _lineage(SUMMARY, ["shop.order_items"], C, VIEW_SOURCE),
        _lineage(SUMMARY, ["shop.orders"], H, "overlay:schemaguru.yaml:12"),
    ]


@contextmanager
def lineage_store(path: Path) -> Iterator[SQLiteStore]:
    with shop_store(path, [*shop_facts(), *lineage_facts()]) as s:
        s.upsert_tables([Table(REVENUE, "view"), Table(PAID, "view"), Table(SUMMARY, "view")])
        s.upsert_columns(REVENUE, [Column(REVENUE_COL, 1, "NUMERIC", "number")])
        s.upsert_columns(PAID, [Column(c("v_paid_orders", "amount"), 1, "NUMERIC", "number")])
        s.upsert_columns(SUMMARY, [Column(c("order_summary", "n"), 1, "INTEGER", "integer")])
        yield s
