"""DuckDB reader: ``duckdb_tables()/duckdb_views()/duckdb_columns()/duckdb_constraints()``.

Works in the connection's current database (``list_catalogs`` yields nothing), so refs carry no
catalog. DuckDB enforces PK/UNIQUE/FK, hence ``enforced=True``.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime
from typing import Any

from schemaguru.catalog.executor import MetadataExecutor
from schemaguru.catalog.protocol import Page, ReaderCapabilities
from schemaguru.catalog.readers._common import definition_text
from schemaguru.catalog.readers._types import family_of
from schemaguru.catalog.registry import register_reader
from schemaguru.model.refs import ObjectRef
from schemaguru.model.structure import Column, Constraint, ConstraintKind, Schema, Table

_SYSTEM_SCHEMAS = ("information_schema", "pg_catalog")
_KINDS: Mapping[str, ConstraintKind] = {
    "PRIMARY KEY": "primary_key",
    "UNIQUE": "unique",
    "FOREIGN KEY": "foreign_key",
}


def _pairs(tables: Sequence[ObjectRef], table_col: str = "table_name") -> tuple[str, list[str]]:
    clause = " OR ".join(f"(schema_name = ? AND {table_col} = ?)" for _ in tables)
    params: list[str] = []
    for t in tables:
        params += [t.schema or "main", t.table]
    return clause, params


class DuckDBReader:
    dialect = "duckdb"

    def __init__(
        self,
        executor: MetadataExecutor,
        *,
        page_size: int = 1000,
        timeout_s: float | None = None,
    ) -> None:
        self._ex = executor
        self.page_size = page_size
        self.timeout_s = timeout_s

    def list_catalogs(self) -> Iterator[str]:
        return iter(())

    def list_schemas(self, catalog: str | None, *, page_token: str | None) -> Page[Schema]:
        if page_token is not None:  # one page holds every schema: a later page is empty
            return Page([], None)
        rows = self._ex.run(
            "SELECT schema_name, comment FROM duckdb_schemas() "
            "WHERE database_name = current_database() "
            "AND schema_name NOT IN (?, ?) ORDER BY schema_name",
            _SYSTEM_SCHEMAS,
            timeout_s=self.timeout_s,
        )
        return Page([Schema(None, r["schema_name"], r["comment"]) for r in rows], None)

    def list_tables(
        self, schema: Schema, *, page_token: str | None, changed_since: datetime | None
    ) -> Page[Table]:
        rows = self._ex.run(
            "SELECT table_name, kind, comment, estimated_size FROM ("
            " SELECT table_name, 'table' AS kind, comment, estimated_size, schema_name,"
            "  database_name FROM duckdb_tables() WHERE NOT temporary"
            " UNION ALL"
            " SELECT view_name, 'view', comment, NULL, schema_name, database_name"
            "  FROM duckdb_views() WHERE NOT internal AND NOT temporary) t "
            "WHERE database_name = current_database() AND schema_name = ? "
            "AND (CAST(? AS VARCHAR) IS NULL OR table_name > ?) ORDER BY table_name LIMIT ?",
            (schema.name, page_token, page_token, self.page_size + 1),
            timeout_s=self.timeout_s,
        )
        more = len(rows) > self.page_size
        rows = rows[: self.page_size]
        items = [
            Table(
                ObjectRef(None, schema.name, r["table_name"]),
                r["kind"],
                r["comment"],
                None if r["estimated_size"] is None else int(r["estimated_size"]),
            )
            for r in rows
        ]
        return Page(items, rows[-1]["table_name"] if more else None)

    def columns(self, tables: Sequence[ObjectRef]) -> Mapping[ObjectRef, list[Column]]:
        out: dict[ObjectRef, list[Column]] = {t.table_ref(): [] for t in tables}
        if not tables:
            return out
        clause, params = _pairs(tables)
        rows = self._ex.run(
            "SELECT schema_name, table_name, column_name, column_index, comment, "
            "column_default, is_nullable, data_type FROM duckdb_columns() "
            f"WHERE database_name = current_database() AND ({clause}) "
            "ORDER BY schema_name, table_name, column_index",
            params,
            timeout_s=self.timeout_s,
        )
        for r in rows:
            tref = ObjectRef(None, r["schema_name"], r["table_name"])
            out.setdefault(tref, []).append(
                Column(
                    ObjectRef(None, r["schema_name"], r["table_name"], r["column_name"]),
                    int(r["column_index"]),
                    r["data_type"],
                    family_of(r["data_type"]),
                    r["is_nullable"],
                    r["comment"],
                    r["column_default"],
                )
            )
        return out

    def constraints(self, tables: Sequence[ObjectRef]) -> Mapping[ObjectRef, list[Constraint]]:
        out: dict[ObjectRef, list[Constraint]] = {t.table_ref(): [] for t in tables}
        if not tables:
            return out
        clause, params = _pairs(tables)
        rows: list[dict[str, Any]] = self._ex.run(
            "SELECT schema_name, table_name, constraint_type, constraint_column_names, "
            "referenced_table, referenced_column_names FROM duckdb_constraints() "
            f"WHERE database_name = current_database() AND ({clause}) "
            "AND constraint_type IN ('PRIMARY KEY', 'UNIQUE', 'FOREIGN KEY') "
            "ORDER BY schema_name, table_name, constraint_index",
            params,
            timeout_s=self.timeout_s,
        )
        for r in rows:
            kind = _KINDS[r["constraint_type"]]
            tref = ObjectRef(None, r["schema_name"], r["table_name"])
            ref_table = ref_cols = None
            if kind == "foreign_key":
                ref_table = ObjectRef(None, r["schema_name"], r["referenced_table"])
                ref_cols = tuple(r["referenced_column_names"])
            out.setdefault(tref, []).append(
                Constraint(
                    kind, tref, tuple(r["constraint_column_names"]), ref_table, ref_cols, True
                )
            )
        return out

    def view_definitions(self, tables: Sequence[ObjectRef]) -> Mapping[ObjectRef, str]:
        """``duckdb_views().sql``: the full ``CREATE VIEW`` statement (internal views excluded)."""
        out: dict[ObjectRef, str] = {}
        if not tables:
            return out
        clause, params = _pairs(tables, "view_name")
        rows = self._ex.run(
            "SELECT schema_name, view_name, sql FROM duckdb_views() "
            f"WHERE database_name = current_database() AND NOT internal AND ({clause}) "
            "ORDER BY schema_name, view_name",
            params,
            timeout_s=self.timeout_s,
        )
        for r in rows:
            text = definition_text(r["sql"])
            if text is not None:
                out[ObjectRef(None, r["schema_name"], r["view_name"])] = text
        return out

    def capabilities(self) -> ReaderCapabilities:
        return ReaderCapabilities(
            comments=True, stats=True, constraints=True, view_definitions=True
        )


register_reader("duckdb", DuckDBReader)
