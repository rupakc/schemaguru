from __future__ import annotations

import os
from typing import Any

import pytest

from tests.fixtures.shop import ShopFacts, build_shop
from tests.fixtures.shop.build import SPECS


def one(conn: Any, sql: str) -> Any:
    return conn.execute(sql).fetchone()[0]


def join_facts(conn: Any, left: str, lc: str, right: str, rc: str, cast: bool = False):
    lk, rk = (f"CAST({lc} AS VARCHAR)", f"CAST({rc} AS VARCHAR)") if cast else (lc, rc)
    nn = one(conn, f"SELECT count({lc}) FROM shop.{left}")
    dl = one(conn, f"SELECT count(DISTINCT {lc}) FROM shop.{left}")
    mk = one(
        conn,
        f"SELECT count(DISTINCT {lk}) FROM shop.{left} WHERE {lc} IS NOT NULL AND "
        f"{lk} IN (SELECT {rk} FROM shop.{right})",
    )
    mr = one(
        conn,
        f"SELECT count(*) FROM shop.{left} WHERE {lc} IS NOT NULL AND "
        f"{lk} IN (SELECT {rk} FROM shop.{right})",
    )
    rd = one(conn, f"SELECT count(DISTINCT {rc}) FROM shop.{right}")
    return nn, dl, mk, mr, rd


def test_shop_builds_and_matches_expected(shop_duckdb: tuple[Any, ShopFacts]) -> None:
    conn, f = shop_duckdb
    for t in SPECS:
        assert one(conn, f"SELECT count(*) FROM shop.{t}") == f.row_counts[t], t
    # exact known numbers (literals pin the brief, not just the builder)
    assert f.row_counts["customers"] == 1000 and f.row_counts["orders"] == 10_000
    assert f.row_counts["empty_t"] == 0 and f.row_counts["big_t"] == 2_000_000
    pairs = {
        "orders->customers": ("orders", "customer_id", "customers", "customer_id", False),
        "orders->accounts": ("orders", "account_id", "accounts", "account_id", False),
        "events->accounts": ("events", "account_id", "accounts", "account_id", False),
        "order_items->orders": ("order_items", "order_id", "orders", "order_id", False),
        "order_items->products": ("order_items", "product_id", "products", "product_id", False),
        "payments->orders": ("payments", "order_id", "orders", "order_id", False),
        "events->sessions": ("events", "session_id", "sessions", "session_id", False),
        "events->orders": ("events", "session_id", "orders", "order_id", True),
        "ref_child->ref_parent": ("ref_child", "parent_id", "ref_parent", "id", False),
        "broken_child->ref_parent": ("broken_child", "parent_id", "ref_parent", "id", False),
    }
    assert set(pairs) == set(f.joins)
    for name, args in pairs.items():
        j = f.joins[name]
        got = join_facts(conn, *args)
        assert got == (
            j.left_non_null,
            j.distinct_left,
            j.matched_keys,
            j.matched_rows,
            j.right_distinct,
        ), name
    oc = f.joins["orders->customers"]
    assert (oc.matched_rows, oc.row_match_rate) == (9980, 0.998)
    assert (oc.distinct_left, oc.matched_keys) == (1020, 1000)
    assert f.joins["payments->orders"].coverage == 0.05
    assert f.joins["payments->orders"].key_match_rate == 1.0
    assert f.joins["ref_child->ref_parent"].key_match_rate == 1.0
    assert f.joins["ref_child->ref_parent"].coverage == 0.003
    assert f.joins["broken_child->ref_parent"].key_match_rate == 0.01
    assert f.joins["events->orders"].key_match_rate == 0.0
    assert f.hub_fan_in == {"orders.account_id": 40, "events.account_id": 60}
    assert f.joins["orders->accounts"].distinct_left == 40
    assert f.joins["events->accounts"].distinct_left == 60


def test_traps(shop_duckdb: tuple[Any, ShopFacts]) -> None:
    conn, f = shop_duckdb
    rows = dict(conn.execute("SELECT status, count(*) FROM shop.orders GROUP BY 1").fetchall())
    assert rows == f.status_counts and rows["PAID"] == 412
    assert set(rows) == {"paid", "refunded", "cancelled", "PAID"}
    assert one(conn, "SELECT count(*) FROM shop.orders WHERE deleted") == f.deleted_true == 300
    assert one(conn, "SELECT count(*) FROM shop.orders WHERE deleted IS NULL") == f.deleted_null
    assert one(conn, "SELECT count(*) FROM shop.orders WHERE deleted = FALSE") == 0
    assert f.deleted_false == 0
    for key, n in f.null_counts.items():
        t, c = key.split(".")
        assert one(conn, f"SELECT count(*) FROM shop.{t} WHERE {c} IS NULL") == n
    assert f.null_rates["all_null_t.c"] == 1.0 and f.null_rates["empty_t.v"] is None
    for key, n in f.distinct_counts.items():
        t, c = key.split(".")
        assert one(conn, f"SELECT count(DISTINCT {c}) FROM shop.{t}") == n
    assert (
        dict(conn.execute("SELECT segment, count(*) FROM shop.customers GROUP BY 1").fetchall())
        == f.segment_counts
    )
    assert (
        one(conn, "SELECT count(*) FROM shop.customers WHERE email LIKE 'user%@example.com'")
        == 1000
    )
    assert (
        one(
            conn,
            "SELECT count(*) FROM shop.anon_enum_t "
            "WHERE regexp_matches(kind, '^[0-9a-f]{32}@anon\\.example$')",
        )
        == 10_000
    )
    assert one(conn, "SELECT min(created_at) FROM shop.orders") == f.created_at_min
    assert one(conn, "SELECT max(created_at) FROM shop.orders") == f.created_at_max
    assert one(conn, "SELECT typeof(created_at) FROM shop.orders LIMIT 1") == "VARCHAR"
    assert (
        one(
            conn,
            "SELECT count(*) FROM (SELECT 1 FROM shop.order_items "
            "GROUP BY order_id, line_no HAVING count(*) > 1)",
        )
        == 0
    )
    assert one(conn, "SELECT count(DISTINCT order_id) FROM shop.order_items") == 10_000


def test_build_is_deterministic() -> None:
    duckdb = pytest.importorskip("duckdb")
    a, b = duckdb.connect(":memory:"), duckdb.connect(":memory:")
    assert build_shop(a, "duckdb") == build_shop(b, "duckdb")
    q = "SELECT order_id, customer_id, status, deleted FROM shop.orders ORDER BY 1"
    assert a.execute(q).fetchall() == b.execute(q).fetchall()


def test_big_t(shop_duckdb_big: Any) -> None:
    assert one(shop_duckdb_big, "SELECT count(*) FROM shop.big_t") == 2_000_000


def test_shop_store_mirrors_ddl(shop_store: Any, shop_facts: ShopFacts) -> None:
    from schemaguru.model.refs import ObjectRef

    tables = {t.ref.table for t in shop_store.iter_tables()}
    assert tables == set(SPECS) | {"big_t"}
    oi = ObjectRef(None, "shop", "order_items")
    pk = [c for c in shop_store.constraints(oi) if c.kind == "primary_key"]
    assert pk[0].columns == ("order_id", "line_no")
    fks = [
        c
        for c in shop_store.constraints(ObjectRef(None, "shop", "orders"))
        if c.kind == "foreign_key"
    ]
    assert {c.columns for c in fks} == {("customer_id",), ("account_id",)}
    assert shop_store.table(ObjectRef(None, "shop", "orders")).row_count == 10_000


@pytest.mark.integration
@pytest.mark.skipif(
    not os.environ.get("SCHEMAGURU_TEST_POSTGRES_DSN"), reason="SCHEMAGURU_TEST_POSTGRES_DSN unset"
)
def test_shop_builds_on_postgres() -> None:
    psycopg = pytest.importorskip("psycopg")
    with psycopg.connect(os.environ["SCHEMAGURU_TEST_POSTGRES_DSN"], autocommit=True) as conn:
        f = build_shop(conn, "postgres")
        for t in SPECS:
            n = conn.execute(f"SELECT count(*) FROM shop.{t}").fetchone()[0]
            assert n == f.row_counts[t], t
