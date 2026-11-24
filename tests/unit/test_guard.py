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
    # quoted names are case-folded for ban decisions
    assert not check_sql('SELECT * FROM shop."PAYMENTS"', never_query=nq).ok
    assert not check_sql('SELECT * FROM "PAYMENTS"', never_query=nq).ok


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT '''it's'''; DROP TABLE t; -- '",
        "SELECT 1 # '\n; DROP TABLE t; -- '",
        "SELECT $$'$$; DROP TABLE t; -- '",
        "SELECT $a$'$a$ AS x; DROP TABLE t; SELECT 1 -- '",
        "SELECT 1 /* /* */ ' */ ; DROP TABLE t; --'",
        "SELECT 1 // '\n; DROP TABLE t; --'",
        "SELECT 1 /*! ; DROP TABLE t */",
        "SELECT 1 --1; DROP TABLE t",
    ],
)
def test_guard_lexer_bypasses_rejected(sql: str) -> None:
    v = check_sql(sql)
    assert not v.ok and v.reasons


NQ = [ObjectRef(None, "analytics", "secret")]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM proj.analytics.secret",
        "SELECT * FROM `proj.analytics.secret`",
        "SELECT * FROM ONLY secret",
        "SELECT 1 FROM t WINDOW w AS (PARTITION BY secret)",
        "SELECT * FROM (secret s CROSS JOIN t)",
        "SELECT * FROM a JOIN b ON a.id = b.id, secret",
        "SELECT * FROM secret AS x(c1, c2)",
        "SELECT * FROM t, secret TABLESAMPLE SYSTEM (10)",
        "SELECT * FROM t, LATERAL (SELECT 1) l, secret",
        "SELECT * FROM t, UNNEST(x) u, secret",
        "SELECT * FROM secret FOR SYSTEM_TIME AS OF now()",
        "SELECT * FROM TABLE(secret)",
        "SELECT * FROM TABLE('analytics.secret')",
        "SELECT * FROM IDENTIFIER('analytics.secret')",
        "WITH secret AS (SELECT 1) SELECT * FROM secret",
        'SELECT * FROM "SECRET"',
        "SELECT * FROM `proj`.`Analytics`.`Secret`",
    ],
)
def test_guard_never_query_fails_closed(sql: str) -> None:
    v = check_sql(sql, never_query=NQ)
    assert not v.ok and any(r.startswith("SG802") for r in v.reasons)


def test_guard_never_query_negative_control() -> None:
    assert check_sql(
        "SELECT * FROM proj.analytics.orders o JOIN customers c ON 1=1", never_query=NQ
    ).ok
