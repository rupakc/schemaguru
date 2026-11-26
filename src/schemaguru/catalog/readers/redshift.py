"""Redshift reader over the ``SVV_*`` system views and ``pg_constraint`` (spec 6.2).

Catalog = database (``SVV_ALL_*`` span databases, including datashares), schema = schema.
Local tables come from ``SVV_ALL_TABLES/COLUMNS``; row counts and size from ``SVV_TABLE_INFO``
(current database only; ``tbl_rows`` includes rows deleted but not yet vacuumed, ``size`` is in
1 MB blocks). External (Spectrum) schemas: tables are flagged via ``SVV_EXTERNAL_TABLES`` and a
table that ``SVV_ALL_COLUMNS`` does not describe is read from ``SVV_EXTERNAL_COLUMNS``.
Constraints are informational (not enforced); ``pg_constraint`` is read per column position.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime
from typing import Any

from schemaguru.catalog.executor import MetadataExecutor
from schemaguru.catalog.protocol import Page, ReaderCapabilities
from schemaguru.catalog.readers._common import Row, lower_keys, opt_int, pairs, yes_no
from schemaguru.catalog.readers._types import family_of
from schemaguru.catalog.registry import register_reader
from schemaguru.model.refs import ObjectRef
from schemaguru.model.structure import Column, Constraint, ConstraintKind, Schema, Table, TableKind

_MB = 1024 * 1024
_KINDS: Mapping[str, TableKind] = {
    "TABLE": "table",
    "BASE TABLE": "table",
    "VIEW": "view",
    "MATERIALIZED VIEW": "materialized_view",
    "EXTERNAL TABLE": "external",
}
_CONTYPE: Mapping[str, ConstraintKind] = {
    "p": "primary_key",
    "u": "unique",
    "f": "foreign_key",
}
_SYSTEM_SCHEMAS = (
    "information_schema",
    "pg_catalog",
    "pg_internal",
    "pg_automv",
    "catalog_history",
)


def _rs_family(native: str) -> str:
    head = native.strip().lower()
    if head == "super":
        return "json"
    if head.startswith(("varbyte", "binary varying")):
        return "bytes"
    if head.startswith("hllsketch"):
        return "other"
    return family_of(native)


def _native(r: Row) -> str:
    dt = str(r["data_type"])
    if dt.lower().startswith("character") and r.get("character_maximum_length") is not None:
        return f"{dt}({int(r['character_maximum_length'])})"
    if dt.lower() in ("numeric", "decimal") and r.get("numeric_precision") is not None:
        return f"{dt}({int(r['numeric_precision'])},{int(r['numeric_scale'] or 0)})"
    return dt


class RedshiftReader:
    dialect = "redshift"

    def __init__(
        self,
        executor: MetadataExecutor,
        *,
        database: str | None = None,
        page_size: int = 1000,
        timeout_s: float | None = None,
    ) -> None:
        self._ex = executor
        self.database = database
        self.page_size = page_size
        self.timeout_s = timeout_s
        self._current_db: str | None = None

    def _run(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        return lower_keys(self._ex.run(sql, params, timeout_s=self.timeout_s))

    def _current(self) -> str:
        if self._current_db is None:
            rows = self._run("SELECT CURRENT_DATABASE() AS name")
            self._current_db = str(rows[0]["name"])
        return self._current_db

    def _db(self, catalog: str | None) -> str:
        if catalog:
            return catalog
        if self.database is None:
            self.database = self._current()
        return self.database

    def list_catalogs(self) -> Iterator[str]:
        return iter((self._db(None),))

    def list_schemas(self, catalog: str | None, *, page_token: str | None) -> Page[Schema]:
        if page_token is not None:  # one page holds every schema: a later page is empty
            return Page([], None)
        db = self._db(catalog)
        marks = ", ".join("?" for _ in _SYSTEM_SCHEMAS)
        rows = self._run(
            "SELECT s.schema_name AS schema_name, "
            "(SELECT COUNT(*) FROM SVV_ALL_TABLES t WHERE t.database_name = s.database_name "
            " AND t.schema_name = s.schema_name) AS table_count "
            "FROM SVV_ALL_SCHEMAS s "
            f"WHERE s.database_name = ? AND s.schema_name NOT IN ({marks}) "
            "AND s.schema_name NOT LIKE ? "
            "ORDER BY s.schema_name",
            (db, *_SYSTEM_SCHEMAS, "pg\\_%"),
        )
        return Page([Schema(db, r["schema_name"], None, opt_int(r["table_count"])) for r in rows])

    def list_tables(
        self, schema: Schema, *, page_token: str | None, changed_since: datetime | None
    ) -> Page[Table]:
        db = self._db(schema.catalog)
        where = ""
        params: list[Any] = [db, schema.name]
        if page_token is not None:
            where = "AND t.table_name > ? "
            params.append(page_token)
        params.append(self.page_size + 1)
        # Leader-node (SVV_ALL_*, SVV_EXTERNAL_*) and compute-node (SVV_TABLE_INFO) views
        # cannot be joined reliably: separate queries, merged here.
        rows = self._run(
            "SELECT t.table_name AS table_name, t.table_type AS table_type, "
            "t.remarks AS remarks FROM SVV_ALL_TABLES t "
            f"WHERE t.database_name = ? AND t.schema_name = ? {where}"
            "ORDER BY t.table_name LIMIT ?",
            params,
        )
        info: dict[str, Row] = {}
        external: set[str] = set()
        if rows and db == self._current():
            info = {
                r["table_name"]: r
                for r in self._run(
                    'SELECT "table" AS table_name, tbl_rows, "size" AS size_mb '
                    'FROM SVV_TABLE_INFO WHERE "schema" = ?',
                    (schema.name,),
                )
            }
            external = {
                r["table_name"]
                for r in self._run(
                    "SELECT tablename AS table_name FROM SVV_EXTERNAL_TABLES WHERE schemaname = ?",
                    (schema.name,),
                )
            }
        more = len(rows) > self.page_size
        rows = rows[: self.page_size]
        items: list[Table] = []
        for r in rows:
            kind = _KINDS.get(str(r["table_type"]).upper(), "other")
            if r["table_name"] in external:
                kind = "external"
            i = info.get(r["table_name"], {})
            size = opt_int(i.get("size_mb"))
            items.append(
                Table(
                    ObjectRef(db, schema.name, r["table_name"]),
                    kind,
                    r["remarks"] or None,
                    opt_int(i.get("tbl_rows")),
                    None if size is None else size * _MB,
                )
            )
        return Page(items, rows[-1]["table_name"] if more else None)

    def columns(self, tables: Sequence[ObjectRef]) -> Mapping[ObjectRef, list[Column]]:
        out: dict[ObjectRef, list[Column]] = {t.table_ref(): [] for t in tables}
        if not tables:
            return out
        by_db: dict[str, list[ObjectRef]] = {}
        for t in tables:
            by_db.setdefault(self._db(t.catalog), []).append(t)
        for db, group in by_db.items():
            clause, params = pairs(group, "schema_name", "table_name")
            rows = self._run(
                "SELECT schema_name, table_name, column_name, ordinal_position, data_type, "
                "is_nullable, column_default, remarks, character_maximum_length, "
                "numeric_precision, numeric_scale FROM SVV_ALL_COLUMNS "
                f"WHERE database_name = ? AND ({clause}) "
                "ORDER BY schema_name, table_name, ordinal_position",
                (db, *params),
            )
            for r in rows:
                out.setdefault(ObjectRef(db, r["schema_name"], r["table_name"]), []).append(
                    Column(
                        ObjectRef(db, r["schema_name"], r["table_name"], r["column_name"]),
                        int(r["ordinal_position"]),
                        _native(r),
                        _rs_family(str(r["data_type"])),
                        yes_no(r["is_nullable"]),
                        r["remarks"] or None,
                        r["column_default"],
                    )
                )
            missing = [t for t in group if not out.get(t.table_ref())]
            if missing and db == self._current():  # SVV_EXTERNAL_* see the current database only
                self._external_columns(db, missing, out)
        return out

    def _external_columns(
        self, db: str, missing: Sequence[ObjectRef], out: dict[ObjectRef, list[Column]]
    ) -> None:
        clause, params = pairs(missing, "schemaname", "tablename")
        rows = self._run(
            "SELECT schemaname, tablename, columnname, columnnum, external_type, is_nullable "
            f"FROM SVV_EXTERNAL_COLUMNS WHERE ({clause}) "
            "ORDER BY schemaname, tablename, columnnum",
            params,
        )
        for r in rows:
            out.setdefault(ObjectRef(db, r["schemaname"], r["tablename"]), []).append(
                Column(
                    ObjectRef(db, r["schemaname"], r["tablename"], r["columnname"]),
                    int(r["columnnum"]),
                    r["external_type"],
                    _rs_family(str(r["external_type"])),
                    yes_no(r["is_nullable"]),
                )
            )

    def constraints(self, tables: Sequence[ObjectRef]) -> Mapping[ObjectRef, list[Constraint]]:
        out: dict[ObjectRef, list[Constraint]] = {t.table_ref(): [] for t in tables}
        if not tables:
            return out
        by_db: dict[str, list[ObjectRef]] = {}
        for t in tables:
            by_db.setdefault(self._db(t.catalog), []).append(t)
        for db, group in by_db.items():
            if db != self._current():
                continue  # pg_constraint covers the current database only: unknown, not guessed
            clause, params = pairs(group, "n.nspname", "c.relname")
            rows = self._run(
                "SELECT n.nspname AS schema_name, c.relname AS table_name, "
                "con.conname AS constraint_name, con.contype AS contype, k.i AS pos, "
                "a.attname AS column_name, fn.nspname AS ref_schema, fc.relname AS ref_table, "
                "fa.attname AS ref_column "
                "FROM pg_constraint con JOIN pg_class c ON c.oid = con.conrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "JOIN (SELECT generate_series(1, 32) AS i) k ON k.i <= array_upper(con.conkey, 1) "
                "JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = con.conkey[k.i] "
                "LEFT JOIN pg_class fc ON fc.oid = con.confrelid "
                "LEFT JOIN pg_namespace fn ON fn.oid = fc.relnamespace "
                "LEFT JOIN pg_attribute fa ON fa.attrelid = con.confrelid "
                "AND fa.attnum = con.confkey[k.i] "
                f"WHERE con.contype IN (?, ?, ?) AND ({clause}) "
                "ORDER BY n.nspname, c.relname, con.conname, k.i",
                ("p", "u", "f", *params),
            )
            grouped: dict[tuple[str, str, str], list[Row]] = {}
            for r in rows:
                grouped.setdefault(
                    (r["schema_name"], r["table_name"], r["constraint_name"]), []
                ).append(r)
            for (schema, table, _name), grp in grouped.items():
                kind = _CONTYPE[grp[0]["contype"]]
                tref = ObjectRef(db, schema, table)
                ref_table = ref_cols = None
                if kind == "foreign_key":
                    if any(g["ref_column"] is None for g in grp):
                        continue
                    ref_table = ObjectRef(db, grp[0]["ref_schema"], grp[0]["ref_table"])
                    ref_cols = tuple(g["ref_column"] for g in grp)
                out.setdefault(tref, []).append(
                    Constraint(
                        kind, tref, tuple(g["column_name"] for g in grp), ref_table, ref_cols, False
                    )
                )
        return out

    def capabilities(self) -> ReaderCapabilities:
        return ReaderCapabilities(comments=True, stats=True, constraints=True)


register_reader("redshift", RedshiftReader)
