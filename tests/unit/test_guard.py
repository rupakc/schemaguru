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
    # a literal naming the table is refused: dynamic SQL is built from strings
    assert not check_sql("SELECT 'FROM shop.payments' FROM shop.orders", never_query=nq).ok
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


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1 -- x\r; DROP TABLE t",
        "SELECT 1 --\r; DROP TABLE t",
        "SELECT /* /* */ \" */ length($$'$$) # 1; DROP TABLE t; --' --\"",
        "SELECT 1 AS [']; DROP TABLE t; --']",
        'SELECT * FROM U&"\\0073ecret"',
        "SELECT * FROM U&'\\0073ecret'",
        "SELECT * FROM `analytics.\\x73ecret`",
        "SELECT query_to_xml('select * from analytics.sec'||'ret', true, true, '')",
        "SELECT table_to_xml('analytics.secret'::regclass, true, true, '')",
        "SELECT * FROM query_table('secret')",
        "SELECT * FROM IDENTIFIER($tbl)",
        "SELECT * FROM dblink('x', 'select 1') AS t(a int)",
        "SELECT * FROM openquery(srv, 'select 1')",
    ],
)
def test_guard_round2_bypasses_rejected(sql: str) -> None:
    v = check_sql(sql, never_query=NQ)
    assert not v.ok and v.reasons
    assert not check_sql(sql).ok or "secret" in sql or "analytics" in sql  # generic-only control


def test_guard_ambiguous_reason() -> None:
    v = check_sql("SELECT 1 -- x\r; DROP TABLE t")
    assert v.reasons == ("SG801: ambiguous across SQL dialects",)


def test_guard_crlf_comment_still_passes() -> None:
    assert check_sql("SELECT 1 -- note\r\nFROM t\r\n").ok


REALISTIC = [
    "WITH recent AS (SELECT id, created_at FROM orders WHERE created_at > '2026-01-01') "
    "SELECT * FROM recent",
    "SELECT id, SUM(total) OVER (PARTITION BY customer_id ORDER BY created_at) FROM orders",
    "SELECT CASE WHEN total > 100 THEN 'big' ELSE 'small' END AS size FROM orders",
    "SELECT payload, '$.items[0].sku' AS path FROM events",
    "SELECT * FROM events WHERE name ~ r'#\\d+'",
    "SELECT * FROM users WHERE note LIKE '%--%'",
    "SELECT * FROM users WHERE note LIKE '%#%'",
    "SELECT * FROM users WHERE note LIKE '%//%'",
    "SELECT * FROM users WHERE note LIKE '50!%' ESCAPE '!'",
    "SELECT /* the daily revenue */ SUM(total) FROM orders -- trailing note\n",
    "SELECT * FROM orders WHERE id = $1",
    "SELECT * FROM secret_santa_events",
    "SELECT secret_count FROM sessions",
]


@pytest.mark.parametrize("sql", REALISTIC)
def test_guard_realistic_queries_pass(sql: str) -> None:
    v = check_sql(sql, never_query=[ObjectRef(None, "analytics", "secret")])
    assert v.ok, v.reasons


def test_guard_performance_at_cap() -> None:
    import time

    unit = "SELECT a, b FROM t1 WHERE c = 'x''y' /* c */ -- n\n"
    sql = (unit * (100_000 // len(unit)))[:99_000]
    nq = [ObjectRef(None, "s", f"banned_{i}") for i in range(50)]
    best = 9.0
    for _ in range(3):
        t0 = time.perf_counter()
        check_sql(sql, never_query=nq)
        best = min(best, time.perf_counter() - t0)
    assert best < 0.5


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT x[1] FROM t",
        "SELECT arr[OFFSET(0)] FROM t",
        "SELECT arr[SAFE_ORDINAL(1)] FROM t",
        "SELECT a[1:2] FROM t",
        'SELECT "col[1]" FROM t',
    ],
)
def test_guard_array_subscripts_pass(sql: str) -> None:
    v = check_sql(sql)
    assert v.ok, v.reasons


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1 AS [']; DROP TABLE t; --']",
        "SELECT [-- ]\n; DROP TABLE t",
        "SELECT 1 FROM t WHERE x = [--]; DROP TABLE t",
    ],
)
def test_guard_bracket_quote_bypasses_stay_rejected(sql: str) -> None:
    assert not check_sql(sql).ok
