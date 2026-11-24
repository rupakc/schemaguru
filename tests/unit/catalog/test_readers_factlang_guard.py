"""Every catalog reader's metadata SQL passes FactLang's own metadata guard (FL703).

``Guru.connect`` and ``Guru(connector=...)`` run catalog SQL through ``FactLangExecutor`` ->
``fl.connectors.run_metadata``, whose guard is lexer-free and deliberately over-strict: no
backslash, ``$``, ``#``, ``--``, ``;`` anywhere and, on T-SQL, no quote at all. A reader query
it refuses makes the crawl skip that catalog/schema (``SG102``) on a real warehouse while the
replay and DB-API tests stay green, so this checks the text of every query each reader sends.
"""

from __future__ import annotations

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
    return cap.sql


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
