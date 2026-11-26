"""Every catalog reader's metadata SQL passes FactLang's own metadata guard (FL703).

``Guru.connect`` and ``Guru(connector=...)`` run catalog SQL through ``FactLangExecutor`` ->
``fl.connectors.run_metadata``, whose guard is lexer-free and deliberately over-strict: no
backslash, ``$``, ``#``, ``--``, ``;`` anywhere and, on T-SQL, no quote at all. A reader query
it refuses makes the crawl skip that catalog/schema (``SG102``) on a real warehouse while the
replay and DB-API tests stay green, so this checks the text of every query each reader sends.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

import pytest
from factlang.connectors import check_metadata_sql

import schemaguru as sg
from schemaguru.catalog import reader_for
from schemaguru.catalog.executor import _factlang_sql
from schemaguru.catalog.executor import check_metadata_sql as sg_guard
from schemaguru.model.refs import ObjectRef
from schemaguru.model.structure import Schema


class _Capture:
    """Records every query (as FactLangExecutor converts it) and answers with no rows."""

    def __init__(self) -> None:
        self.sql: list[str] = []

    def run(
        self, sql: str, params: Sequence[Any] = (), *, timeout_s: float | None = None
    ) -> list[dict[str, Any]]:
        sg_guard(sql)
        text, _ = _factlang_sql(sql, len(params))
        self.sql.append(text)
        if sql.lower().startswith("select current_database()"):  # which database is current
            return [{"name": "db"}]
        return []


def _drive(dialect: str) -> list[str]:
    cap = _Capture()
    reader = reader_for(dialect, cap)
    catalog = "db" if dialect in ("bigquery", "snowflake", "databricks", "trino") else None
    list(reader.list_catalogs())
    reader.list_schemas(catalog, page_token=None)
    reader.list_tables(Schema(catalog, "shop"), page_token=None, changed_since=None)
    reader.list_tables(Schema(catalog, "shop"), page_token="t", changed_since=None)
    ref = ObjectRef(catalog, "shop", "orders")
    reader.columns([ref])
    reader.constraints([ref])
    if reader.capabilities().view_definitions:
        reader.view_definitions([ref])  # type: ignore[attr-defined]
    return cap.sql


def _drive_view_definitions(dialect: str) -> list[str]:
    cap = _Capture()
    reader = reader_for(dialect, cap)
    catalog = "db" if dialect in ("bigquery", "snowflake", "databricks", "trino") else None
    assert reader.capabilities().view_definitions, dialect
    reader.view_definitions([ObjectRef(catalog, "shop", "v_open_orders")])  # type: ignore[attr-defined]
    return cap.sql


# What a view-definition read may name after FROM/JOIN: catalog relations only, per dialect.
# ``check_metadata_sql`` has no relation allow-list, so this pins it.
CATALOG_RELATIONS: dict[str, set[str]] = {
    "postgres": {"pg_views", "pg_matviews"},
    "redshift": {"pg_views"},
    "duckdb": {"duckdb_views"},
    "snowflake": {"information_schema.views"},
    "bigquery": {"information_schema.views", "information_schema.tables"},
    "databricks": {"information_schema.views"},
    "mysql": {"information_schema.views"},
    "clickhouse": {"system.tables"},
    "tsql": {"sys.sql_modules", "sys.objects", "sys.schemas"},
    "oracle": {"all_views", "all_mviews"},
    "trino": {"information_schema.views"},
}


def _relations(sql: str) -> set[str]:
    from schemaguru._util.sqltext import table_uses

    out: set[str] = set()
    for use in table_uses(sql):
        names = [str(p[0] if isinstance(p, tuple) else p).strip('`"').lower() for p in use.parts]
        # the project/dataset/database qualifier is data; the catalog relation is what follows
        for keep in (2, 1):
            if names[-keep:] and names[-keep:][0] in ("information_schema", "system", "sys"):
                out.add(".".join(names[-keep:]))
                break
        else:
            out.add(names[-1])
    # table functions (DuckDB's ``duckdb_views()``) are not tables to ``table_uses``
    out |= set(re.findall(r"(?:from|join)\s+([a-z_.]+)\s*\(", sql.lower()))
    return out


@pytest.mark.parametrize("dialect", sg.catalog.dialects())
def test_reader_sql_passes_the_factlang_metadata_guard(dialect: str) -> None:
    queries = _drive(dialect)
    assert queries, dialect
    for text in queries:
        check_metadata_sql(text, dialect)  # FL703 here = a crawl that fails on a real warehouse


def test_the_guard_check_can_fail() -> None:
    """Negative control: the queries the readers used to send are refused."""
    from factlang.connectors import ExecutionError

    with pytest.raises(ExecutionError, match="FL703"):
        check_metadata_sql("SELECT n.nspname FROM pg_namespace n WHERE n.nspname NOT LIKE 'pg\\_%'")
    with pytest.raises(ExecutionError, match="FL703"):
        check_metadata_sql("SELECT o.name FROM sys.objects o WHERE o.type IN ('U', 'V')", "tsql")


def test_every_dialect_has_a_catalog_relation_allow_list() -> None:
    assert set(CATALOG_RELATIONS) == set(sg.catalog.dialects())


@pytest.mark.parametrize("dialect", sg.catalog.dialects())
def test_view_definition_sql_reads_catalog_only(dialect: str) -> None:
    queries = _drive_view_definitions(dialect)
    assert queries, dialect
    seen: set[str] = set()
    for text in queries:
        check_metadata_sql(text, dialect)
        seen |= _relations(text)
    assert seen and seen <= CATALOG_RELATIONS[dialect], (dialect, seen)


def test_the_relation_check_can_fail() -> None:
    """Negative control: a user-table read is flagged."""
    assert _relations("SELECT * FROM shop.orders") == {"orders"}
    assert not {"orders"} <= CATALOG_RELATIONS["postgres"]
