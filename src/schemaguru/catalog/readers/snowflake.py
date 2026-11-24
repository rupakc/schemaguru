"""Snowflake reader over ``<db>.INFORMATION_SCHEMA`` plus ``SHOW ... KEYS IN SCHEMA`` (spec 6.2).

Catalog = database, schema = schema. Identifiers are stored exactly as Snowflake returns them
(upper-case unless they were created quoted), and are re-quoted verbatim when interpolated.
``INFORMATION_SCHEMA`` is metadata-only but runs on the session's warehouse (compute credits);
``SHOW`` commands do not need one. Primary/unique/foreign keys are declared, **not enforced**.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime
from typing import Any

from schemaguru.catalog.executor import MetadataExecutor
from schemaguru.catalog.protocol import Page, ReaderCapabilities
from schemaguru.catalog.readers._common import (
    Row,
    group_by_scope,
    lower_keys,
    opt_int,
    quote_ident,
    to_utc,
    yes_no,
)
from schemaguru.catalog.readers._types import family_of
from schemaguru.catalog.registry import register_reader
from schemaguru.model.refs import ObjectRef
from schemaguru.model.structure import Column, Constraint, ConstraintKind, Schema, Table, TableKind

_KINDS: Mapping[str, TableKind] = {
    "BASE TABLE": "table",
    "VIEW": "view",
    "MATERIALIZED VIEW": "materialized_view",
    "EXTERNAL TABLE": "external",
}
_CLUSTER = re.compile(r"^\s*(?:LINEAR\s*)?\(\s*([^()]*?)\s*\)\s*$", re.IGNORECASE)


def _sf_family(data_type: str, scale: Any) -> str:
    head = data_type.strip().upper()
    if head in ("NUMBER", "DECIMAL", "NUMERIC"):
        return "integer" if opt_int(scale) == 0 else "decimal"
    if head in ("FLOAT", "FLOAT4", "FLOAT8", "DOUBLE", "REAL"):
        return "float"
    if head in ("TEXT", "VARCHAR", "CHAR", "STRING"):
        return "string"
    if head in ("VARIANT",):
        return "json"
    if head == "OBJECT":
        return "struct"
    if head == "ARRAY":
        return "array"
    if head in ("BINARY", "VARBINARY"):
        return "bytes"
    if head.startswith("TIMESTAMP"):
        return "timestamp"
    return family_of(data_type)


def _native(r: Row) -> str:
    dt = str(r["data_type"])
    head = dt.upper()
    if head == "NUMBER" and r.get("numeric_precision") is not None:
        return f"NUMBER({int(r['numeric_precision'])},{int(r['numeric_scale'] or 0)})"
    if head == "TEXT" and r.get("character_maximum_length") is not None:
        return f"TEXT({int(r['character_maximum_length'])})"
    return dt


def _clustering(key: Any) -> tuple[str, ...] | None:
    """``LINEAR(A, B)`` -> ``('A', 'B')``; anything with an expression is left unknown."""
    if not isinstance(key, str):
        return None
    m = _CLUSTER.match(key)
    if m is None:
        return None
    parts = tuple(p.strip() for p in m.group(1).split(","))
    return parts if all(parts) else None


class SnowflakeReader:
    dialect = "snowflake"

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

    def _run(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        return lower_keys(self._ex.run(sql, params, timeout_s=self.timeout_s))

    def _db(self, catalog: str | None) -> str:
        db = catalog or self.database
        if db is None:
            rows = self._run("SELECT CURRENT_DATABASE() AS name")
            db = rows[0]["name"] if rows else None
        if not db:
            raise ValueError("no database: pass database= or set one on the session")
        self.database = self.database or db
        return str(db)

    def list_catalogs(self) -> Iterator[str]:
        return iter((self._db(None),))

    def list_schemas(self, catalog: str | None, *, page_token: str | None) -> Page[Schema]:
        db = self._db(catalog)
        rows = self._run(
            "SELECT schema_name, comment FROM "
            f"{quote_ident(db)}.INFORMATION_SCHEMA.SCHEMATA "
            "WHERE schema_name <> ? ORDER BY schema_name",
            ("INFORMATION_SCHEMA",),
        )
        return Page([Schema(db, r["schema_name"], r["comment"]) for r in rows])

    def list_tables(
        self, schema: Schema, *, page_token: str | None, changed_since: datetime | None
    ) -> Page[Table]:
        db = self._db(schema.catalog)
        rows = self._run(
            "SELECT table_name, table_type, row_count, bytes, created, last_altered, comment, "
            "clustering_key "
            f"FROM {quote_ident(db)}.INFORMATION_SCHEMA.TABLES "
            "WHERE table_schema = ? "
            "AND (CAST(? AS VARCHAR) IS NULL OR table_name > ?) "
            "AND (CAST(? AS TIMESTAMP_TZ) IS NULL OR last_altered >= ?) "
            "ORDER BY table_name LIMIT ?",
            (schema.name, page_token, page_token, changed_since, changed_since, self.page_size + 1),
        )
        more = len(rows) > self.page_size
        rows = rows[: self.page_size]
        items = [
            Table(
                ObjectRef(db, schema.name, r["table_name"]),
                _KINDS.get(str(r["table_type"]).upper(), "other"),
                r["comment"] or None,
                opt_int(r["row_count"]),
                opt_int(r["bytes"]),
                created_at=to_utc(r["created"]),
                last_altered=to_utc(r["last_altered"]),
                clustering=_clustering(r["clustering_key"]),
            )
            for r in rows
        ]
        return Page(items, rows[-1]["table_name"] if more else None)

    def columns(self, tables: Sequence[ObjectRef]) -> Mapping[ObjectRef, list[Column]]:
        out: dict[ObjectRef, list[Column]] = {t.table_ref(): [] for t in tables}
        for (catalog, schema), names in group_by_scope(tables).items():
            if schema is None:
                continue
            db = self._db(catalog)
            marks = ", ".join("?" for _ in names)
            rows = self._run(
                "SELECT table_name, column_name, ordinal_position, data_type, is_nullable, "
                "column_default, comment, numeric_precision, numeric_scale, "
                "character_maximum_length "
                f"FROM {quote_ident(db)}.INFORMATION_SCHEMA.COLUMNS "
                f"WHERE table_schema = ? AND table_name IN ({marks}) "
                "ORDER BY table_name, ordinal_position",
                (schema, *names),
            )
            for r in rows:
                out.setdefault(ObjectRef(db, schema, r["table_name"]), []).append(
                    Column(
                        ObjectRef(db, schema, r["table_name"], r["column_name"]),
                        int(r["ordinal_position"]),
                        _native(r),
                        _sf_family(str(r["data_type"]), r["numeric_scale"]),
                        yes_no(r["is_nullable"]),
                        r["comment"] or None,
                        r["column_default"],
                    )
                )
        return out

    def constraints(self, tables: Sequence[ObjectRef]) -> Mapping[ObjectRef, list[Constraint]]:
        out: dict[ObjectRef, list[Constraint]] = {t.table_ref(): [] for t in tables}
        for (catalog, schema), names in group_by_scope(tables).items():
            if schema is None:
                continue
            db = self._db(catalog)
            wanted = set(names)
            scope = f"{quote_ident(db)}.{quote_ident(schema)}"
            kinds: tuple[tuple[ConstraintKind, str], ...] = (
                ("primary_key", "PRIMARY KEYS"),
                ("unique", "UNIQUE KEYS"),
            )
            for kind, show in kinds:
                rows = self._run(f"SHOW {show} IN SCHEMA {scope}")
                for (table, _n), cols in _keys(rows, "table_name", "constraint_name").items():
                    if table in wanted:
                        out.setdefault(ObjectRef(db, schema, table), []).append(
                            Constraint(kind, ObjectRef(db, schema, table), cols, None, None, False)
                        )
            fks = self._run(f"SHOW IMPORTED KEYS IN SCHEMA {scope}")
            for table, fk_name in _keys(fks, "fk_table_name", "fk_name"):
                if table not in wanted:
                    continue
                grp = [r for r in fks if r["fk_table_name"] == table and r["fk_name"] == fk_name]
                grp.sort(key=lambda r: int(r["key_sequence"]))
                ref = ObjectRef(
                    grp[0]["pk_database_name"], grp[0]["pk_schema_name"], grp[0]["pk_table_name"]
                )
                out.setdefault(ObjectRef(db, schema, table), []).append(
                    Constraint(
                        "foreign_key",
                        ObjectRef(db, schema, table),
                        tuple(r["fk_column_name"] for r in grp),
                        ref,
                        tuple(r["pk_column_name"] for r in grp),
                        False,
                    )
                )
        return out

    def capabilities(self) -> ReaderCapabilities:
        return ReaderCapabilities(
            comments=True, stats=True, last_altered=True, constraints=True, incremental=True
        )


def _keys(
    rows: Sequence[Row], table_key: str, name_key: str
) -> dict[tuple[str, str], tuple[str, ...]]:
    """Group SHOW ... KEYS rows by (table, constraint) and order columns by ``key_sequence``."""
    grouped: dict[tuple[str, str], list[tuple[int, str]]] = {}
    for r in rows:
        col = str(r.get("column_name", r.get("fk_column_name")))
        grouped.setdefault((r[table_key], r[name_key]), []).append((int(r["key_sequence"]), col))
    return {k: tuple(c for _, c in sorted(v)) for k, v in grouped.items()}


register_reader("snowflake", SnowflakeReader)
