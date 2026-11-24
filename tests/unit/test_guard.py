from __future__ import annotations

import pytest

from schemaguru.guard import check_sql
from schemaguru.model import ObjectRef


def test_guard_accepts_plain_select_and_with() -> None:
    assert check_sql("SELECT id FROM shop.orders WHERE id = 1;").ok
    assert check_sql("WITH x AS (SELECT 1) SELECT * FROM x").ok


def test_guard_literal_like_not_rejected() -> None:
    v = check_sql("SELECT id FROM users WHERE email LIKE '%@x.y' AND note = 'DELETE; DROP'")
    assert v.ok and v.reasons == ()


def test_guard_comment_does_not_hide_or_trigger() -> None:
    assert check_sql("SELECT 1 -- DROP TABLE x\n").ok
    assert not check_sql("SELECT 1 /* it's */ ; DROP TABLE x").ok
    assert not check_sql("-- don't\nSELECT 1; DELETE FROM x").ok


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM orders",
        "SELECT 1; SELECT 2",
        "SELECT 1; DROP TABLE x",
        "SELECT * INTO t2 FROM orders",
        "SHOW TABLES",
        "DESCRIBE orders",
        "",
        "   ",
        "UPDATE orders SET a = 1",
        "WITH d AS (DELETE FROM orders RETURNING *) SELECT * FROM d",
    ],
)
def test_guard_rejects_delete_and_multi_statement(sql: str) -> None:
    v = check_sql(sql)
    assert not v.ok and v.reasons
    assert all(r.startswith("SG801") or r.startswith("SG802") for r in v.reasons)


def test_guard_length_cap() -> None:
    assert not check_sql("SELECT '" + "x" * 100_000 + "'").ok


def test_guard_backslash_escape_cannot_hide_a_statement() -> None:
    assert not check_sql("SELECT 'a\\'' ; DROP TABLE x --").ok
    assert not check_sql("SELECT E'it\\'s INSERT'").ok  # ambiguous: refused conservatively
    assert check_sql("SELECT 'it''s'").ok
    assert not check_sql("SELECT 'a\\' ; DROP TABLE x; --'").ok


def test_guard_backtick_identifier_cannot_start_a_comment() -> None:
    assert not check_sql("SELECT `a--b` FROM t; DELETE FROM t").ok


def test_guard_never_query_table() -> None:
    nq = [ObjectRef(None, "shop", "payments")]
    assert not check_sql("SELECT * FROM shop.payments", never_query=nq).ok
    assert not check_sql("SELECT * FROM SHOP.Payments p", never_query=nq).ok
    assert not check_sql("SELECT * FROM orders o JOIN payments p ON 1=1", never_query=nq).ok
    assert not check_sql("SELECT * FROM orders, shop.payments", never_query=nq).ok
    v = check_sql('SELECT * FROM "shop"."payments"', never_query=nq)
    assert not v.ok and v.reasons[0].startswith("SG802")
    assert check_sql("SELECT * FROM shop.orders", never_query=nq).ok
    # a literal mentioning the table is not a reference
    assert check_sql("SELECT 'FROM shop.payments' FROM shop.orders", never_query=nq).ok
    # a double-quoted part is case-exact
    assert check_sql('SELECT * FROM shop."PAYMENTS"', never_query=nq).ok
