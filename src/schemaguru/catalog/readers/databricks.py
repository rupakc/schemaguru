"""Databricks reader over Unity Catalog ``system.information_schema`` (spec 6.2).

Three-level namespace: catalog.schema.table. One ``system.information_schema`` serves every
catalog the principal may see, so a single reader needs no per-catalog connection. Names are
lower-case as stored. Unity Catalog exposes no row counts or sizes in ``information_schema``
(those need ``DESCRIBE DETAIL`` per table), so ``row_count``/``bytes`` stay unknown. PK/FK
constraints are informational (not enforced). Requires ``USE CATALOG``/``USE SCHEMA`` and
``SELECT`` on the system tables (``system.information_schema`` is granted to account users by
default).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime
from typing import Any

from schemaguru.catalog.executor import MetadataExecutor
from schemaguru.catalog.protocol import Page, ReaderCapabilities
from schemaguru.catalog.readers._common import Row, lower_keys, pairs3, to_utc, yes_no
from schemaguru.catalog.readers._types import family_of
from schemaguru.catalog.registry import register_reader
from schemaguru.model.refs import ObjectRef
from schemaguru.model.structure import Column, Constraint, ConstraintKind, Schema, Table, TableKind

_IS = "system.information_schema"
_SKIP_CATALOGS = ("system", "__databricks_internal")
_KINDS: Mapping[str, TableKind] = {
    "MANAGED": "table",
    "EXTERNAL": "external",
    "FOREIGN": "external",
    "VIEW": "view",
    "MATERIALIZED_VIEW": "materialized_view",
    "STREAMING_TABLE": "table",
    "MANAGED_SHALLOW_CLONE": "table",
    "EXTERNAL_SHALLOW_CLONE": "external",
}
_CONSTRAINTS: Mapping[str, ConstraintKind] = {
    "PRIMARY KEY": "primary_key",
    "UNIQUE": "unique",
    "FOREIGN KEY": "foreign_key",
}


def _dbx_family(full_type: str) -> str:
    head = full_type.strip().lower()
    if head.startswith(("map<", "struct<")):
        return "struct"
    if head.startswith("array<"):
        return "array"
    if head in ("string", "variant") or head.startswith(("varchar", "char")):
        return "json" if head == "variant" else "string"
    if head in ("int", "bigint", "smallint", "tinyint", "long", "short", "byte"):
        return "integer"
    if head == "timestamp_ntz":
        return "timestamp"
    if head.startswith("interval"):
        return "other"
    return family_of(full_type)


class DatabricksReader:
    dialect = "databricks"

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

    def _run(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        return lower_keys(self._ex.run(sql, params, timeout_s=self.timeout_s))

    def list_catalogs(self) -> Iterator[str]:
        marks = ", ".join("?" for _ in _SKIP_CATALOGS)
        rows = self._run(
            f"SELECT catalog_name FROM {_IS}.catalogs "
            f"WHERE catalog_name NOT IN ({marks}) ORDER BY catalog_name",
            _SKIP_CATALOGS,
        )
        return iter([r["catalog_name"] for r in rows])

    def list_schemas(self, catalog: str | None, *, page_token: str | None) -> Page[Schema]:
        if page_token is not None:  # one page holds every schema: a later page is empty
            return Page([], None)
        where = "" if catalog is None else "AND s.catalog_name = ? "
        rows = self._run(
            "SELECT s.catalog_name AS catalog_name, s.schema_name AS schema_name, "
            "s.comment AS comment FROM "
            f"{_IS}.schemata s WHERE s.schema_name <> ? {where}"
            "ORDER BY s.catalog_name, s.schema_name",
            ("information_schema",) if catalog is None else ("information_schema", catalog),
        )
        return Page([Schema(r["catalog_name"], r["schema_name"], r["comment"]) for r in rows])

    def list_tables(
        self, schema: Schema, *, page_token: str | None, changed_since: datetime | None
    ) -> Page[Table]:
        where = ""
        params: list[Any] = [schema.catalog, schema.name]
        if page_token is not None:
            where += "AND table_name > ? "
            params.append(page_token)
        if changed_since is not None:
            where += "AND last_altered >= ? "
            params.append(changed_since)
        params.append(self.page_size + 1)
        rows = self._run(
            "SELECT table_name, table_type, comment, created, last_altered "
            f"FROM {_IS}.tables WHERE table_catalog = ? AND table_schema = ? "
            f"{where}ORDER BY table_name LIMIT ?",
            params,
        )
        more = len(rows) > self.page_size
        rows = rows[: self.page_size]
        items = [
            Table(
                ObjectRef(schema.catalog, schema.name, r["table_name"]),
                _KINDS.get(str(r["table_type"]).upper(), "other"),
                r["comment"] or None,
                created_at=to_utc(r["created"]),
                last_altered=to_utc(r["last_altered"]),
            )
            for r in rows
        ]
        return Page(items, rows[-1]["table_name"] if more else None)

    def columns(self, tables: Sequence[ObjectRef]) -> Mapping[ObjectRef, list[Column]]:
        out: dict[ObjectRef, list[Column]] = {t.table_ref(): [] for t in tables}
        if not tables:
            return out
        clause, params = pairs3(tables, "table_catalog", "table_schema", "table_name")
        rows = self._run(
            "SELECT table_catalog, table_schema, table_name, column_name, ordinal_position, "
            "full_data_type, is_nullable, column_default, comment "
            f"FROM {_IS}.columns WHERE {clause} "
            "ORDER BY table_catalog, table_schema, table_name, ordinal_position",
            params,
        )
        for r in rows:
            cat, sch, tab = r["table_catalog"], r["table_schema"], r["table_name"]
            out.setdefault(ObjectRef(cat, sch, tab), []).append(
                Column(
                    ObjectRef(cat, sch, tab, r["column_name"]),
                    int(r["ordinal_position"]) + 1,  # Unity Catalog counts from 0
                    r["full_data_type"],
                    _dbx_family(str(r["full_data_type"])),
                    yes_no(r["is_nullable"]),
                    r["comment"] or None,
                    r["column_default"],
                )
            )
        return out

    def constraints(self, tables: Sequence[ObjectRef]) -> Mapping[ObjectRef, list[Constraint]]:
        out: dict[ObjectRef, list[Constraint]] = {t.table_ref(): [] for t in tables}
        if not tables:
            return out
        clause, params = pairs3(tables, "k.table_catalog", "k.table_schema", "k.table_name")
        rows = self._run(
            "SELECT k.table_catalog AS table_catalog, k.table_schema AS table_schema, "
            "k.table_name AS table_name, k.constraint_name AS constraint_name, "
            "tc.constraint_type AS constraint_type, k.column_name AS column_name, "
            "k.ordinal_position AS ordinal_position, pk.table_catalog AS ref_catalog, "
            "pk.table_schema AS ref_schema, pk.table_name AS ref_table, "
            "pk.column_name AS ref_column "
            f"FROM {_IS}.key_column_usage k "
            f"JOIN {_IS}.table_constraints tc ON tc.constraint_catalog = k.constraint_catalog "
            "AND tc.constraint_schema = k.constraint_schema "
            "AND tc.constraint_name = k.constraint_name "
            f"LEFT JOIN {_IS}.referential_constraints rc "
            "ON rc.constraint_catalog = k.constraint_catalog "
            "AND rc.constraint_schema = k.constraint_schema "
            "AND rc.constraint_name = k.constraint_name "
            f"LEFT JOIN {_IS}.key_column_usage pk "
            "ON pk.constraint_catalog = rc.unique_constraint_catalog "
            "AND pk.constraint_schema = rc.unique_constraint_schema "
            "AND pk.constraint_name = rc.unique_constraint_name "
            "AND pk.ordinal_position = k.position_in_unique_constraint "
            f"WHERE {clause} "
            "ORDER BY k.table_catalog, k.table_schema, k.table_name, k.constraint_name, "
            "k.ordinal_position",
            params,
        )
        grouped: dict[tuple[str, str, str, str], list[Row]] = {}
        for r in rows:
            key = (r["table_catalog"], r["table_schema"], r["table_name"], r["constraint_name"])
            grouped.setdefault(key, []).append(r)
        for (cat, sch, tab, _name), grp in grouped.items():
            kind = _CONSTRAINTS.get(str(grp[0]["constraint_type"]).upper())
            if kind is None:
                continue
            tref = ObjectRef(cat, sch, tab)
            ref_table = ref_cols = None
            if kind == "foreign_key":
                if any(g["ref_column"] is None for g in grp):
                    continue
                ref_table = ObjectRef(
                    grp[0]["ref_catalog"], grp[0]["ref_schema"], grp[0]["ref_table"]
                )
                ref_cols = tuple(g["ref_column"] for g in grp)
            out.setdefault(tref, []).append(
                Constraint(
                    kind, tref, tuple(g["column_name"] for g in grp), ref_table, ref_cols, False
                )
            )
        return out

    def capabilities(self) -> ReaderCapabilities:
        return ReaderCapabilities(
            comments=True, last_altered=True, constraints=True, incremental=True
        )


register_reader("databricks", DatabricksReader)
