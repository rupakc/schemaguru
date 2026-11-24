from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from schemaguru.measure.catalog_bridge import StoreCatalog
from schemaguru.model.refs import ObjectRef
from schemaguru.store.sqlite import SQLiteStore


@dataclass(frozen=True)
class Ident:  # the shape of factlang.ir.Ident (duck-typed: no factlang import)
    text: str
    quoted: bool = False


@dataclass(frozen=True)
class TableRef:
    catalog: Ident | None
    schema: Ident | None
    table: Ident


def tr(schema: str, table: str, quoted: bool = False) -> TableRef:
    return TableRef(None, Ident(schema), Ident(table, quoted))


def test_store_catalog_answers_from_crawled_columns(shop_store: SQLiteStore) -> None:
    cat = StoreCatalog(shop_store)
    assert cat.table_exists(tr("shop", "orders")) is True
    cols = cat.columns(tr("shop", "orders"))
    assert cols is not None and cols["customer_id"] == "INTEGER" and "status" in cols
    # unquoted parts match case-insensitively (a unique match); quoted ones exactly
    assert cat.table_exists(tr("SHOP", "Orders")) is True
    assert cat.table_exists(tr("shop", "Orders", quoted=True)) is None


def test_store_catalog_unknown_is_none(shop_store: SQLiteStore) -> None:
    cat = StoreCatalog(shop_store)
    # an un-crawled table is unknown, never "absent" (FactLang then emits FL313 info)
    assert cat.table_exists(tr("shop", "nope")) is None
    assert cat.columns(tr("shop", "nope")) is None
    assert cat.table_exists(TableRef(None, None, Ident("orders"))) is None


def test_store_catalog_withdrawn_is_false(shop_store: SQLiteStore) -> None:
    shop_store.mark_withdrawn(
        [ObjectRef(None, "shop", "payments")], datetime(2026, 11, 1, tzinfo=timezone.utc)
    )
    cat = StoreCatalog(shop_store)
    assert cat.table_exists(tr("shop", "payments")) is False
    assert cat.columns(tr("shop", "payments")) is None
