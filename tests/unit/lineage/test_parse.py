"""The in-process lineage parser (0.2.0 spec 4.1, 4.4)."""

from __future__ import annotations

import logging
from typing import Any

import pytest

pytest.importorskip("sqlglot")

import contextlib

from schemaguru.lineage.parse import (
    MAX_DEFINITION_CHARS,
    ColumnLineage,
    LineageFailure,
    ParsedLineage,
    extract_tables,
    lineage_available,
    parse_lineage,
)
from schemaguru.model.lineage import ResolvedTable

SCHEMA = {
    (None, "shop", "orders"): ResolvedTable("shop.orders", ("order_id", "amount", "status")),
    (None, "shop", "refunds"): ResolvedTable("shop.refunds", ("order_id", "amount")),
}
WIDE = {
    **SCHEMA,
    (None, "shop", "orders"): ResolvedTable(
        "shop.orders", ("order_id", "amount", "status", "tags")
    ),
}

ORD, REF = "shop.orders", "shop.refunds"
FAMILIES = ("postgres", "snowflake", "bigquery", "tsql", "clickhouse")


def _cols(p: ParsedLineage) -> dict[str, tuple[tuple[str, ...], str]]:
    return {c.column: (c.inputs, c.transform) for c in p.columns}


# construct -> (sql, schema, expected_upstream, expected_columns); a dialect override replaces sql
_CONSTRUCTS: dict[str, tuple[str, Any, tuple[str, ...], dict[str, Any]]] = {
    "copy": (
        "SELECT o.order_id, o.amount AS amt FROM shop.orders o",
        SCHEMA, (ORD,),
        {"order_id": ((f"{ORD}.order_id",), "copy"), "amt": ((f"{ORD}.amount",), "copy")},
    ),
    "cast_fn": (
        "SELECT CAST(o.amount AS INT) AS a FROM shop.orders o",
        SCHEMA, (ORD,), {"a": ((f"{ORD}.amount",), "cast")},
    ),
    "cast_colons": (
        "SELECT o.amount::INT AS a FROM shop.orders o",
        SCHEMA, (ORD,), {"a": ((f"{ORD}.amount",), "cast")},
    ),
    "try_cast": (
        "SELECT TRY_CAST(o.amount AS INT) AS a FROM shop.orders o",
        SCHEMA, (ORD,), {"a": ((f"{ORD}.amount",), "cast")},
    ),
    "expression": (
        "SELECT o.amount * 2 AS dbl, CASE WHEN o.status = 'x' THEN 1 END AS flag "
        "FROM shop.orders o",
        SCHEMA, (ORD,),
        {"dbl": ((f"{ORD}.amount",), "expression"), "flag": ((f"{ORD}.status",), "expression")},
    ),
    "aggregate": (
        "SELECT o.status, COUNT(*) AS n, SUM(o.amount) AS total FROM shop.orders o "
        "GROUP BY o.status",
        SCHEMA, (ORD,),
        {
            "status": ((f"{ORD}.status",), "copy"),
            "n": ((), "aggregate"),
            "total": ((f"{ORD}.amount",), "aggregate"),
        },
    ),
    "window": (
        "SELECT o.order_id, ROW_NUMBER() OVER (PARTITION BY o.status ORDER BY o.amount) AS rn, "
        "SUM(o.amount) OVER (PARTITION BY o.status) AS running FROM shop.orders o",
        SCHEMA, (ORD,),
        {
            "order_id": ((f"{ORD}.order_id",), "copy"),
            "rn": ((f"{ORD}.amount", f"{ORD}.status"), "window"),
            "running": ((f"{ORD}.amount", f"{ORD}.status"), "window"),
        },
    ),
    "cte": (
        "WITH big AS (SELECT order_id, amount FROM shop.orders WHERE amount > 10) "
        "SELECT b.order_id, b.amount * 2 AS dbl FROM big b",
        SCHEMA, (ORD,),
        {"order_id": ((f"{ORD}.order_id",), "copy"), "dbl": ((f"{ORD}.amount",), "expression")},
    ),
    "recursive_cte": (
        "WITH RECURSIVE r (n) AS (SELECT order_id FROM shop.orders UNION ALL "
        "SELECT n + 1 FROM r WHERE n < 5) SELECT n FROM r",
        SCHEMA, (ORD,), {"n": ((f"{ORD}.order_id",), "expression")},
    ),
    "subquery": (
        "SELECT s.total FROM (SELECT SUM(amount) AS total FROM shop.orders) s",
        SCHEMA, (ORD,), {"total": ((f"{ORD}.amount",), "aggregate")},
    ),
    "union_all": (
        "SELECT order_id, amount FROM shop.orders UNION ALL SELECT order_id, amount AS x "
        "FROM shop.refunds",
        SCHEMA, (ORD, REF),
        {
            "order_id": ((f"{ORD}.order_id", f"{REF}.order_id"), "copy"),
            "amount": ((f"{ORD}.amount", f"{REF}.amount"), "copy"),
        },
    ),
    "intersect": (
        "SELECT order_id FROM shop.orders INTERSECT SELECT order_id FROM shop.refunds",
        SCHEMA, (ORD, REF), {"order_id": ((f"{ORD}.order_id", f"{REF}.order_id"), "copy")},
    ),
    "except": (
        "SELECT order_id FROM shop.orders EXCEPT SELECT order_id FROM shop.refunds",
        SCHEMA, (ORD, REF), {"order_id": ((f"{ORD}.order_id", f"{REF}.order_id"), "copy")},
    ),
    "join": (
        "SELECT o.order_id, r.amount AS refunded FROM shop.orders o "
        "JOIN shop.refunds r ON r.order_id = o.order_id WHERE o.status = 'paid'",
        SCHEMA, (ORD, REF),
        {"order_id": ((f"{ORD}.order_id",), "copy"), "refunded": ((f"{REF}.amount",), "copy")},
    ),
    "unnest": (
        "SELECT o.order_id, u.tag FROM shop.orders o CROSS JOIN LATERAL UNNEST(o.tags) AS u(tag)",
        WIDE, (ORD,),
        {"order_id": ((f"{ORD}.order_id",), "copy"), "tag": ((f"{ORD}.tags",), "expression")},
    ),
    "qualify": (
        "SELECT o.order_id FROM shop.orders o "
        "QUALIFY ROW_NUMBER() OVER (PARTITION BY o.status ORDER BY o.amount) = 1",
        SCHEMA, (ORD,), {"order_id": ((f"{ORD}.order_id",), "copy")},
    ),
    "star": (
        "SELECT * FROM shop.refunds",
        SCHEMA, (REF,),
        {"order_id": ((f"{REF}.order_id",), "copy"), "amount": ((f"{REF}.amount",), "copy")},
    ),
    "unqualified_no_schema": (
        "SELECT order_id FROM shop.orders o JOIN shop.refunds r ON r.order_id = o.order_id",
        {}, (), {"order_id": ((), "unknown")},
    ),
    "unresolved_table": (
        "SELECT o.order_id, g.x FROM shop.orders o JOIN ghost.t g ON g.id = o.order_id",
        SCHEMA, (ORD,), {"order_id": ((f"{ORD}.order_id",), "copy"), "x": ((), "copy")},
    ),
}  # fmt: skip

_OVERRIDES: dict[tuple[str, str], str] = {
    ("snowflake", "unnest"): "SELECT o.order_id, f.value AS tag FROM shop.orders o, "
    "LATERAL FLATTEN(input => o.tags) f",
    ("bigquery", "unnest"): "SELECT o.order_id, tag FROM shop.orders o, UNNEST(o.tags) AS tag",
    ("tsql", "unnest"): "SELECT o.order_id, j.value AS tag FROM shop.orders o "
    "CROSS APPLY OPENJSON(o.tags) j",
    ("clickhouse", "unnest"): "SELECT o.order_id, tag FROM shop.orders o ARRAY JOIN o.tags AS tag",
    ("bigquery", "try_cast"): "SELECT SAFE_CAST(o.amount AS INT64) AS a FROM shop.orders o",
    ("bigquery", "intersect"): "SELECT order_id FROM shop.orders INTERSECT DISTINCT "
    "SELECT order_id FROM shop.refunds",
    ("bigquery", "except"): "SELECT order_id FROM shop.orders EXCEPT DISTINCT "
    "SELECT order_id FROM shop.refunds",
    ("tsql", "recursive_cte"): "WITH r (n) AS (SELECT order_id FROM shop.orders UNION ALL "
    "SELECT n + 1 FROM r WHERE n < 5) SELECT n FROM r",
    ("tsql", "cast_fn"): "SELECT CONVERT(INT, o.amount) AS a FROM shop.orders o",
}
_SKIPS: dict[tuple[str, str], str] = {
    ("postgres", "try_cast"): "no TRY_CAST in postgres",
    ("bigquery", "cast_colons"): "no :: cast in bigquery",
    ("tsql", "cast_colons"): "no :: cast in tsql",
    ("postgres", "qualify"): "no QUALIFY in postgres",
    ("tsql", "qualify"): "no QUALIFY in tsql",
}


def _matrix() -> list[Any]:
    out = []
    for construct, (sql, schema, upstream, columns) in _CONSTRUCTS.items():
        for dialect in FAMILIES:
            key = (dialect, construct)
            case = (dialect, _OVERRIDES.get(key, sql), schema, upstream, columns)
            marks = [pytest.mark.skip(reason=_SKIPS[key])] if key in _SKIPS else []
            out.append(pytest.param(*case, id=f"{construct}-{dialect}", marks=marks))
    return out


@pytest.mark.parametrize(
    ("dialect", "sql", "schema", "expected_upstream", "expected_columns"), _matrix()
)
def test_constructs(
    dialect: str,
    sql: str,
    schema: Any,
    expected_upstream: tuple[str, ...],
    expected_columns: dict[str, Any],
) -> None:
    p = parse_lineage(sql, dialect, schema)
    assert p.upstream == expected_upstream
    assert _cols(p) == expected_columns


def test_brief_example() -> None:
    p = parse_lineage(
        "SELECT o.order_id, SUM(o.amount) - SUM(r.amount) AS net FROM shop.orders o "
        "LEFT JOIN shop.refunds r ON r.order_id = o.order_id WHERE o.status = 'paid' "
        "GROUP BY o.order_id",
        "postgres",
        SCHEMA,
    )
    assert p.upstream == ("shop.orders", "shop.refunds") and p.unresolved == ()
    assert {c.column: (c.inputs, c.transform) for c in p.columns} == {
        "order_id": (("shop.orders.order_id",), "copy"),
        "net": (("shop.orders.amount", "shop.refunds.amount"), "aggregate"),
    }


def test_unresolved_table_names_go_to_unresolved() -> None:
    p = parse_lineage(
        "SELECT o.order_id, g.x FROM shop.orders o JOIN ghost.t g ON g.id = o.order_id",
        "postgres",
        SCHEMA,
    )
    assert p.upstream == ("shop.orders",) and p.unresolved == ("ghost.t",)
    x = next(c for c in p.columns if c.column == "x")
    assert x == ColumnLineage("x", (), "copy", ("ghost.t.x",))
    # a star over an unresolved table yields no columns (spec 2 non-goal)
    assert parse_lineage("SELECT * FROM ghost.t", "postgres", SCHEMA).columns == ()


def test_unknown_column_of_known_table_is_unresolved_not_guessed() -> None:
    p = parse_lineage("SELECT o.nope FROM shop.orders o", "postgres", SCHEMA)
    assert p.columns == (ColumnLineage("nope", (), "copy", ("shop.orders.nope",)),)


def test_unqualified_column_resolved_by_store_columns() -> None:
    p = parse_lineage(
        "SELECT status, r.amount FROM shop.orders o JOIN shop.refunds r ON r.order_id = o.order_id",
        "postgres",
        SCHEMA,
    )
    assert _cols(p) == {
        "status": ((f"{ORD}.status",), "copy"),
        "amount": ((f"{REF}.amount",), "copy"),
    }
    # present in both tables: not guessed
    amb = parse_lineage(
        "SELECT amount FROM shop.orders o JOIN shop.refunds r ON r.order_id = o.order_id",
        "postgres",
        SCHEMA,
    )
    assert _cols(amb) == {"amount": ((), "unknown")}


def test_extract_tables_excludes_ctes_and_recursive_self() -> None:
    sql = (
        "WITH RECURSIVE r AS (SELECT order_id FROM shop.orders UNION ALL SELECT order_id FROM r), "
        "x AS (SELECT * FROM r) SELECT * FROM x JOIN audit.log l ON 1 = 1 "
        "WHERE x.order_id IN (SELECT order_id FROM shop.refunds)"
    )
    assert extract_tables(sql, "postgres") == (
        (None, "audit", "log"),
        (None, "shop", "orders"),
        (None, "shop", "refunds"),
    )
    # a non-recursive CTE that shadows a real table reads the real table in its own body
    assert extract_tables(
        "WITH orders AS (SELECT * FROM orders) SELECT * FROM orders", "postgres"
    ) == ((None, None, "orders"),)


_VIEW_FORMS = [
    ("duckdb", "CREATE VIEW v (oid, amt) AS SELECT order_id, amount FROM shop.orders"),
    ("duckdb", "-- leading comment\nCREATE OR REPLACE VIEW v (oid, amt) AS "
     "SELECT order_id, amount FROM shop.orders;"),
    ("snowflake", "CREATE OR REPLACE SECURE VIEW db1.s.v (oid COMMENT 'the id', amt COMMENT 'x') "
     "COPY GRANTS AS SELECT order_id, amount FROM shop.orders"),
    ("mysql", "CREATE ALGORITHM=UNDEFINED DEFINER=`root`@`%` SQL SECURITY DEFINER VIEW `v` "
     "(`oid`, `amt`) AS select `o`.`order_id` AS `order_id`, `o`.`amount` AS `amount` "
     "from `shop`.`orders` `o`"),
    ("redshift", "CREATE OR REPLACE VIEW v (oid, amt) AS SELECT order_id, amount "
     "FROM shop.orders WITH NO SCHEMA BINDING;"),
    ("postgres", "/* c */ CREATE MATERIALIZED VIEW v (oid, amt) AS "
     "SELECT order_id, amount FROM shop.orders ;"),
]  # fmt: skip


@pytest.mark.parametrize(("dialect", "sql"), _VIEW_FORMS)
def test_parse_accepts_view_wrappers(dialect: str, sql: str) -> None:
    p = parse_lineage(sql, dialect, SCHEMA)
    assert p.upstream == ("shop.orders",)
    assert extract_tables(sql, dialect) == ((None, "shop", "orders"),)
    assert _cols(p) == {"oid": ((f"{ORD}.order_id",), "copy"), "amt": ((f"{ORD}.amount",), "copy")}


def test_view_without_column_list_keeps_names() -> None:
    p = parse_lineage("CREATE VIEW v AS SELECT order_id AS id FROM shop.orders", "duckdb", SCHEMA)
    assert _cols(p) == {"id": ((f"{ORD}.order_id",), "copy")}


def test_star_over_wide_table() -> None:
    cols = tuple(f"c{i:04d}" for i in range(1000))
    p = parse_lineage(
        "SELECT * FROM w.t", "postgres", {(None, "w", "t"): ResolvedTable("w.t", cols)}
    )
    assert len(p.columns) == 1000
    assert all(c.transform == "copy" and c.inputs == (f"w.t.{c.column}",) for c in p.columns)


def test_failures_carry_no_sql() -> None:
    with pytest.raises(LineageFailure) as info:
        parse_lineage("SELECT FROM WHERE 'secret@x.io'", "postgres", SCHEMA)
    assert str(info.value) == "parse_error" and info.value.reason == "parse_error"
    assert "secret" not in repr(info.value)
    assert info.value.__cause__ is None and info.value.__context__ is None


def test_failure_categories() -> None:
    with pytest.raises(LineageFailure, match=r"^unsupported_dialect$"):
        parse_lineage("SELECT 1", "nonsense", SCHEMA)
    with pytest.raises(LineageFailure, match=r"^too_large$"):
        extract_tables("SELECT 1 " + " " * MAX_DEFINITION_CHARS, "postgres")
    with pytest.raises(LineageFailure, match=r"^parse_error$"):
        parse_lineage("CREATE TABLE x (a INT)", "postgres", SCHEMA)
    with pytest.raises(LineageFailure, match=r"^parse_error$"):
        parse_lineage("SELECT 1; SELECT 2", "postgres", SCHEMA)
    deep = "SELECT " + "(" * 5000 + "1" + ")" * 5000
    with pytest.raises(LineageFailure, match=r"^parse_error$"):
        parse_lineage(deep, "postgres", SCHEMA)


def test_sqlglot_logger_silenced_and_restored(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("sqlglot")
    with caplog.at_level(logging.DEBUG, logger="sqlglot"), contextlib.suppress(LineageFailure):
        parse_lineage("SELECT a FROM t WHERE x = 'secret@x.io' FOOBAR BAZ", "postgres", SCHEMA)
    assert "secret" not in caplog.text
    assert logger.disabled is False


def test_columns_false_skips_column_work() -> None:
    p = parse_lineage(
        "SELECT o.order_id FROM shop.orders o JOIN ghost.t g ON 1 = 1",
        "postgres",
        SCHEMA,
        columns=False,
    )
    assert p == ParsedLineage(upstream=("shop.orders",), unresolved=("ghost.t",), columns=())


def test_deterministic_sorted_output() -> None:
    p = parse_lineage(
        "SELECT r.amount, o.status, o.amount FROM shop.orders o, shop.refunds r", "postgres", SCHEMA
    )
    assert [c.column for c in p.columns] == sorted(c.column for c in p.columns)


def test_lineage_available() -> None:
    assert lineage_available() is True


def test_lateral_and_scalar_subqueries() -> None:
    p = parse_lineage(
        "SELECT o.order_id, l.refunded, (SELECT MAX(r.amount) FROM shop.refunds r "
        "WHERE r.order_id = o.order_id) AS top FROM shop.orders o CROSS JOIN LATERAL "
        "(SELECT SUM(r2.amount) AS refunded FROM shop.refunds r2 WHERE r2.order_id = o.order_id) l",
        "postgres",
        SCHEMA,
    )
    assert _cols(p) == {
        "order_id": ((f"{ORD}.order_id",), "copy"),
        "refunded": ((f"{REF}.amount",), "aggregate"),
        "top": ((f"{REF}.amount",), "aggregate"),
    }


def test_star_except_and_cte_column_list() -> None:
    p = parse_lineage(
        "WITH c (a, b) AS (SELECT order_id, amount FROM shop.refunds) SELECT * EXCEPT (b) FROM c",
        "bigquery",
        SCHEMA,
    )
    assert _cols(p) == {"a": ((f"{REF}.order_id",), "copy")}
