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
    # linear work is ~0.2 s locally; shared CI runners (Windows, coverage tracing) reach ~0.6 s.
    # A quadratic guard on 99k characters takes minutes, so 2 s still catches the regression.
    assert best < 2.0


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


# --- fix round 4 -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "nq"),
    [
        ('SELECT 1 AS [a"] ; DROP TABLE t ; SELECT 1 AS ["]', ()),
        ("SELECT [a`] ; DROP TABLE t ; SELECT 1 AS [`]", ()),
        ('SELECT 1 AS [a"], * FROM secret AS ["]', NQ),
    ],
)
def test_guard_r4_identifier_span_must_match_in_every_variant(
    sql: str, nq: list[ObjectRef]
) -> None:
    v = check_sql(sql, never_query=nq)
    assert not v.ok and v.reasons


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT x[1] FROM t",
        "SELECT arr[OFFSET(0)] FROM t",
        "SELECT arr[SAFE_ORDINAL(1)] FROM t",
        "SELECT a[1:2] FROM t",
        'SELECT "col[1]" FROM t',
        'SELECT "a[b]" , x[2] FROM t',
    ],
)
def test_guard_r4_harmless_brackets_still_pass(sql: str) -> None:
    v = check_sql(sql, never_query=NQ)
    assert v.ok, v.reasons


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT x[';'] FROM t",
        "SELECT x[a;b] FROM t",
        "SELECT x[a#b] FROM t",
        "SELECT x[a//b] FROM t",
        "SELECT x[a/*b*/] FROM t",
    ],
)
def test_guard_r4_bracket_region_with_unsafe_content_refused(sql: str) -> None:
    assert check_sql(sql).reasons == ("SG801: ambiguous across SQL dialects",)


@pytest.mark.parametrize(
    "fn",
    [
        "schema_to_xml",
        "schema_to_xmlschema",
        "schema_to_xml_and_xmlschema",
        "database_to_xml",
        "database_to_xmlschema",
        "database_to_xml_and_xmlschema",
        "ts_stat",
        "ts_rewrite",
        "query",
        "dblink_get_result",
        "dblink_open",
        "sqlite_scan",
        "postgres_scan",
        "mysql_scan",
        "parquet_scan",
        "pg_read_file",
        "pg_read_binary_file",
        "lo_import",
        "lo_export",
        "read_csv",
        "read_csv_auto",
        "read_parquet",
        "read_json",
        "read_json_auto",
    ],
)
def test_guard_r4_text_and_file_functions_refused(fn: str) -> None:
    v = check_sql(f"SELECT * FROM {fn}('x', true)")
    assert not v.ok and any(fn in r for r in v.reasons)
    v = check_sql(f'SELECT pg_catalog."{fn}"(1)')
    assert not v.ok


def test_guard_r4_function_names_as_columns_pass() -> None:
    assert check_sql("SELECT query, read_count, ts_rank(v, q) FROM t").ok


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT ts_stat('select v from analytics.sec'\n'ret')",
        "SELECT my_udf('select v from analytics.sec'\n'ret')",
        "SELECT my_udf('select v from analytics.sec' /* c */ 'ret')",
        "SELECT my_udf('select v from analytics.sec' -- c\n 'ret')",
        "SELECT my_udf('select v from analytics.sec' || 'ret')",
    ],
)
def test_guard_r4_adjacent_literals_are_joined(sql: str) -> None:
    v = check_sql(sql, never_query=NQ)
    assert not v.ok and any(r.startswith("SG802") for r in v.reasons)


def test_guard_r4_separate_literals_not_joined_across_code() -> None:
    assert check_sql("SELECT my_udf('sec', x, 'ret')", never_query=NQ).ok


@pytest.mark.parametrize(
    "sql", ["SELECT 1 /*M! ; DROP TABLE t */", "SELECT 1 /*M!100000 , 2 */ FROM t"]
)
def test_guard_r4_mariadb_executable_comment_refused(sql: str) -> None:
    v = check_sql(sql)
    assert not v.ok and v.reasons


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 2 FROM s WHERE 1 --\N{NO-BREAK SPACE}; DROP TABLE t",
        "SELECT 2 FROM s WHERE 1 --\u0085; DROP TABLE t",
        "SELECT 2 FROM s WHERE 1 --\N{EM SPACE}; DROP TABLE t",
    ],
)
def test_guard_r4_dash_dash_space_counts_only_ascii_whitespace(sql: str) -> None:
    v = check_sql(sql)
    assert not v.ok and v.reasons


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT q'[ ' ]' FROM t FOR UPDATE --'",
        "SELECT q'[ ' ]' , 1 ; DROP TABLE t --'",
        "SELECT Q'{ ' }' , 1 ; DROP TABLE t --'",
        "SELECT q'! ' !' , 1 ; DROP TABLE t --'",
        "SELECT nq'< ' >' , 1 ; DROP TABLE t --'",
    ],
)
def test_guard_r4_oracle_q_quote_refused(sql: str) -> None:
    v = check_sql(sql)
    assert not v.ok and v.reasons


def test_guard_r4_single_variant_agreement_covers_every_combination() -> None:
    """Property behind the design: if every single-option variant agrees with the baseline,
    every COMBINATION of options agrees too (checked on random trigger-dense strings)."""
    import dataclasses
    import random

    from schemaguru._util.sqltext import Opts, agreed_lex, agrees, lex

    flags = [f.name for f in dataclasses.fields(Opts) if f.type in (bool, "bool")]
    flags.remove("comments")
    alphabet = [*"'\"`[]-/*#$\\\r\n ;aqQnM!x(", "\N{NO-BREAK SPACE}", "''", "--", "/*", "*/"]
    rng = random.Random(20261124)
    agreed_samples = 0
    for _ in range(30_000):
        sql = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 10)))
        base, ok = agreed_lex(sql)
        if not ok:
            continue
        agreed_samples += 1
        combos = [dict.fromkeys(flags, True)]
        combos += [{f: rng.random() < 0.5 for f in flags} for _ in range(6)]
        for c in combos:
            assert agrees(sql, base, lex(sql, Opts(**c))), (sql, c)
    assert agreed_samples > 1_000


# --- fix round 5: deny by name family ------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM postgres_execute('pg','DROP TABLE t')",
        "SELECT mysql_execute('m','DELETE FROM t')",
        "SELECT * FROM postgres_query('pg','select * from sec'||chr(114)||'et')",
        "SELECT * FROM EXTERNAL_QUERY('conn','select 1')",
        "SELECT * FROM file('/etc/passwd','LineAsString')",
        "SELECT * FROM url('http://169.254.169.254/x','CSV')",
        "SELECT * FROM s3('https://b/k.csv')",
        "SELECT * FROM remote('127.0.0.1', concat('anal','ytics'), concat('sec','ret'))",
        "SELECT * FROM postgresql('h:5432','db','t','u','p')",
        "SELECT * FROM jdbc('jdbc:x','t')",
        "SELECT * FROM odbc('dsn','t')",
        "SELECT * FROM executable('x.sh','CSV','a String')",
        "SELECT * FROM glob('*')",
        "SELECT * FROM read_xlsx('x')",
        "SELECT * FROM parquet_metadata('x')",
        "SELECT * FROM `pg_catalog.query_to_xml`('select 1', true, true, '')",
        "SELECT * FROM `main.read_csv`('x.csv')",
        'SELECT * FROM "main"."READ_CSV"(\'x.csv\')',
        "SELECT * FROM s3Cluster('c','https://b/k')",
        "SELECT * FROM clusterAllReplicas('c', db, t)",
        "SELECT * FROM my_udf_query('x')",
        "SELECT * FROM query_anything('x')",
    ],
)
def test_guard_r5_function_families_refused(sql: str) -> None:
    v = check_sql(sql)
    assert not v.ok and any("named by text" in r for r in v.reasons), v.reasons


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT file, url, query, input, merge_key, read_count, remote_id, cluster_name FROM t",
        "SELECT t.file AS url, t.query AS input FROM t",
        "SELECT count(*), coalesce(a, b), date_trunc('day', ts), json_extract(j, '$.a') FROM t",
        'SELECT "file", `url` FROM t WHERE remote_id = 1',
    ],
)
def test_guard_r5_family_names_not_called_pass(sql: str) -> None:
    v = check_sql(sql, never_query=NQ)
    assert v.ok, v.reasons


def test_guard_r5_weakened_comparison_is_caught_by_the_property() -> None:
    """Negative control for the combination property: gating with a comparison that folds
    identifier kinds into code (the round-3 design) lets SQL through whose all-options lexing
    differs exactly from the baseline. The exact :func:`agrees` gate admits none."""
    import dataclasses
    import random

    from schemaguru._util import sqltext
    from schemaguru._util.sqltext import Opts, agreed_lex, agrees, lex

    weak = str.maketrans("qmcidDejb", "xxccccccc")

    def weak_sig(lx: sqltext.Lexed) -> tuple[str, bool, bool]:
        return lx.kinds.translate(weak), lx.terminated, lx.bang

    def weak_agreed(sql: str) -> tuple[sqltext.Lexed, bool]:
        base = lex(sql, Opts())
        sig = weak_sig(base)
        return base, all(
            not any(t in sql for t in trig) or weak_sig(lex(sql, o)) == sig
            for o, trig in sqltext._VARIANTS
        )

    flags = [f.name for f in dataclasses.fields(Opts) if f.type in (bool, "bool")]
    flags.remove("comments")
    all_on = Opts(**dict.fromkeys(flags, True))
    alphabet = [*"'\"`[]-/*#$\\\r\n ;aqQnM!x(", "''", "--", "/*", "*/"]
    rng = random.Random(20261124)
    weak_misses = strict_misses = 0
    for _ in range(20_000):
        sql = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 10)))
        combo = lex(sql, all_on)
        base, ok = weak_agreed(sql)
        if ok and not agrees(sql, base, combo):
            weak_misses += 1
        base, ok = agreed_lex(sql)
        if ok and not agrees(sql, base, combo):
            strict_misses += 1
    assert weak_misses > 0
    assert strict_misses == 0


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM urlCluster('http://h/x', 'CSV')",
        "SELECT * FROM fileCluster('c', 'x')",
        "SELECT * FROM hdfsCluster('c', 'x')",
        "SELECT * FROM deltaLakeCluster('c', 'x')",
        "SELECT * FROM icebergCluster('c', 'x')",
        "SELECT * FROM s3Cluster('c', 'x')",
        "SELECT * FROM iceberg('s3://b/t')",
        "SELECT * FROM iceberg_scan('s3://b/t')",
        "SELECT * FROM deltalake('s3://b/t')",
        "SELECT * FROM hudi('s3://b/t')",
        "SELECT * FROM paimon('s3://b/t')",
        "SELECT * FROM cosn('cosn://b/t')",
        "SELECT * FROM OSS('oss://b/t')",
        "SELECT postgres_attach('host=x')",
        "SELECT sqlite_attach('x.db')",
        "SELECT getenv('AWS_SECRET_ACCESS_KEY')",
        "SELECT * FROM duckdb_secrets()",
        "SELECT which_secret('s3://b', 's3')",
        "SELECT current_setting('x')",
    ],
)
def test_guard_r6_more_function_families_refused(sql: str) -> None:
    v = check_sql(sql)
    assert not v.ok and any("named by text" in r for r in v.reasons), v.reasons


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM '/etc/passwd'",
        "SELECT * FROM 'http://169.254.169.254/x.csv'",
        "SELECT a FROM t JOIN 'x.parquet' USING (a)",
        "SELECT * FROM LATERAL 'x.csv'",
        "SELECT * FROM ONLY 'x.csv'",
        "SELECT * FROM t LEFT JOIN LATERAL 'x.csv' ON true",
    ],
)
def test_guard_r6_file_as_table_refused(sql: str) -> None:
    v = check_sql(sql)
    assert not v.ok and any("string literal as a table" in r for r in v.reasons), v.reasons


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM t WHERE x = 'a'",
        "SELECT 'a' AS from_x FROM t",
        "SELECT a FROM t WHERE b IN ('from', 'join') AND c = 'x'",
        "SELECT a FROM t JOIN u ON u.k = 'a'",
    ],
)
def test_guard_r6_string_literals_elsewhere_pass(sql: str) -> None:
    v = check_sql(sql)
    assert v.ok, v.reasons


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT json_query(j, '$.a'), json_value(j, '$.b') FROM t WHERE json_exists(j, '$.c')",
        "SELECT JSON_QUERY(j, '$.a' RETURNING text) FROM t",
    ],
)
def test_guard_r6_sql_json_functions_pass(sql: str) -> None:
    v = check_sql(sql)
    assert v.ok, v.reasons


@pytest.mark.parametrize(
    "sql",
    [
        "WITH file(a) AS (SELECT 1) SELECT * FROM file",
        "WITH RECURSIVE input(a) AS (SELECT 1) SELECT * FROM input",
        "SELECT * FROM t AS input(a, b)",
        "WITH read_x(a) AS (SELECT 1) SELECT a FROM read_x",
    ],
)
def test_guard_r6_declarations_are_not_calls(sql: str) -> None:
    v = check_sql(sql)
    assert v.ok, v.reasons


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM t, 'x.csv'",
        "SELECT * FROM t AS a, '/etc/passwd' b",
        "SELECT * FROM (SELECT 1 FROM t, 'x.csv') q",
        "SELECT * FROM (t, 'x.csv')",
        "SELECT * FROM t, LATERAL 'x.csv'",
        "SELECT * FROM t JOIN u ON t.a = u.a, 'x.csv'",
    ],
)
def test_guard_r6b_comma_file_as_table_refused(sql: str) -> None:
    v = check_sql(sql)
    assert not v.ok and any("string literal as a table" in r for r in v.reasons), v.reasons


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 'a', 'b', f(x, 'c') FROM t",
        "SELECT a FROM t WHERE x IN ('a', 'b')",
        "SELECT * FROM (VALUES ('a'), ('b')) v(x)",
        "SELECT * FROM t, u WHERE u.k = 'a' GROUP BY 'a', 'b' ORDER BY 'c', 'd'",
        "SELECT a FROM t WHERE b = 1 LIMIT 1 OFFSET 2",
        "SELECT * FROM t, f('x', 'y')",
        "SELECT x FROM t UNION SELECT 'a', 'b'",
    ],
)
def test_guard_r6b_commas_elsewhere_pass(sql: str) -> None:
    v = check_sql(sql)
    assert v.ok, v.reasons


@pytest.mark.parametrize(
    "sql",
    [
        "WITH a AS (SELECT 1), file(x) AS (SELECT 1) SELECT * FROM file",
        "WITH a AS (SELECT 1), b AS (SELECT 2), read_x(x, y) AS (SELECT 1, 2) SELECT * FROM a",
    ],
)
def test_guard_r6b_comma_cte_declaration_passes(sql: str) -> None:
    v = check_sql(sql)
    assert v.ok, v.reasons


@pytest.mark.parametrize(
    "sql",
    [
        "WITH a AS (SELECT 1), file(x) SELECT * FROM a",
        "WITH a AS (SELECT 1) SELECT read_csv('x'), file('y') FROM a",
        "SELECT 1, file('/etc/passwd')",
    ],
)
def test_guard_r6b_comma_calls_still_refused(sql: str) -> None:
    v = check_sql(sql)
    assert not v.ok and any("named by text" in r for r in v.reasons), v.reasons


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM ('x.csv')",
        "SELECT * FROM t JOIN ('x.csv') ON true",
        "SELECT * FROM t, ('x.csv')",
        "SELECT * FROM (('x.csv'))",
        "SELECT * FROM E'x.csv'",
        "SELECT * FROM t JOIN N'x.csv' ON true",
        "SELECT * FROM t, E'x.csv'",
        "SELECT * FROM U&'x.csv'",
        "SELECT * FROM ('x.csv') AS q",
    ],
)
def test_guard_r6c_parenthesised_and_prefixed_literals_refused(sql: str) -> None:
    v = check_sql(sql)
    assert not v.ok, v.reasons


@pytest.mark.parametrize(
    "sql",
    [
        'SELECT * FROM "/etc/passwd"',
        'SELECT * FROM "data/x"',
        'SELECT * FROM "x.csv"',
        'SELECT * FROM t JOIN "X.PARQUET" ON true',
        'SELECT * FROM t, "x.json.gz"',
        'SELECT * FROM ("x.csv")',
        "SELECT * FROM `x.parquet`",
    ],
)
def test_guard_r6c_path_like_quoted_identifier_refused(sql: str) -> None:
    v = check_sql(sql)
    assert not v.ok and any("path-like" in r for r in v.reasons), v.reasons


@pytest.mark.parametrize(
    "sql",
    [
        'SELECT * FROM "Order Items"',
        'SELECT * FROM "my.table"',
        'SELECT * FROM s."my.table" AS t JOIN "Order Items" o ON true',
        'SELECT "x.csv" FROM t',
        "SELECT * FROM (SELECT 1) q, (VALUES ('a')) v(x)",
        "SELECT * FROM (t JOIN u ON t.a = u.a)",
    ],
)
def test_guard_r6c_ordinary_from_items_pass(sql: str) -> None:
    v = check_sql(sql)
    assert v.ok, v.reasons
