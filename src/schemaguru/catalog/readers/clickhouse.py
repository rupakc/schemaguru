"""ClickHouse reader over ``system.databases / tables / columns`` (spec 6.2).

ClickHouse has no foreign keys and its ``PRIMARY KEY`` is a sparse sort index, not a uniqueness
constraint, so ``constraints()`` returns nothing (never a guessed key). ``Nullable(...)`` and
``LowCardinality(...)`` wrappers are unwrapped before the type family is chosen; a column is
nullable only when ``Nullable`` is present. A database is a schema; refs carry no catalog.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime, timezone

from schemaguru.catalog.executor import MetadataExecutor
from schemaguru.catalog.protocol import Page, ReaderCapabilities
from schemaguru.catalog.readers._common import definition_text
from schemaguru.catalog.readers._rules import blank_to_none, compile_rules, family_by, utc
from schemaguru.catalog.registry import register_reader
from schemaguru.model.refs import ObjectRef
from schemaguru.model.structure import Column, Constraint, Schema, Table, TableKind

_SYSTEM_DATABASES = ("system", "INFORMATION_SCHEMA", "information_schema")
_VIEWS = frozenset({"View", "LiveView", "WindowView"})
_EXTERNAL = frozenset(
    {
        "S3",
        "S3Queue",
        "URL",
        "File",
        "HDFS",
        "Kafka",
        "RabbitMQ",
        "NATS",
        "MySQL",
        "PostgreSQL",
        "MongoDB",
        "Redis",
        "SQLite",
        "ODBC",
        "JDBC",
        "AzureBlobStorage",
        "DeltaLake",
        "Iceberg",
        "Hudi",
    }
)
_NATIVE = frozenset(
    {
        "Memory",
        "Set",
        "Join",
        "Buffer",
        "Distributed",
        "Merge",
        "Null",
        "TinyLog",
        "StripeLog",
        "Log",
        "Dictionary",
    }
)
_RULES = compile_rules(
    (
        (r"^U?Int(8|16|32|64|128|256)\b", "integer"),
        (r"^Decimal", "decimal"),
        (r"^(Float|BFloat)", "float"),
        (r"^(String|FixedString|UUID|Enum|IPv4|IPv6)", "string"),
        (r"^Date(32)?\b", "date"),
        (r"^DateTime", "timestamp"),
        (r"^Array\(", "array"),
        (r"^(Tuple|Map|Nested)\(", "struct"),
        (r"^(JSON|Object)", "json"),
    )
)
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def _unwrap(native: str) -> tuple[str, bool]:
    """Strip ``LowCardinality``/``Nullable`` wrappers; report whether ``Nullable`` was present."""
    nullable = False
    t = native.strip()
    while True:
        for wrapper in ("LowCardinality", "Nullable"):
            if t.startswith(wrapper + "(") and t.endswith(")"):
                nullable = nullable or wrapper == "Nullable"
                t = t[len(wrapper) + 1 : -1].strip()
                break
        else:
            return t, nullable


def _family(inner: str) -> str:
    if inner == "Bool":
        return "boolean"
    return family_by(_RULES, inner)


def _kind(engine: str) -> TableKind:
    if engine in _VIEWS:
        return "view"
    if engine == "MaterializedView":
        return "materialized_view"
    if engine in _EXTERNAL:
        return "external"
    if engine.endswith("MergeTree") or engine in _NATIVE:
        return "table"
    return "other"


def _sorting_columns(key: str | None) -> tuple[str, ...] | None:
    """The sorting key as a column tuple only when every part is a plain identifier."""
    if not key:
        return None
    parts = [p.strip() for p in key.split(",")]
    return tuple(parts) if all(_IDENT.match(p) for p in parts) else None


def _pairs(tables: Sequence[ObjectRef]) -> tuple[str, list[str]]:
    clause = " OR ".join("(database = ? AND `table` = ?)" for _ in tables)
    params: list[str] = []
    for t in tables:
        params += [t.schema or "default", t.table]
    return clause, params


class ClickHouseReader:
    dialect = "clickhouse"

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
            "SELECT d.name AS name, d.comment AS comment, countIf(t.name != '') AS table_count "
            "FROM system.databases d LEFT JOIN system.tables t "
            "ON t.database = d.name AND NOT t.is_temporary "
            "WHERE d.name NOT IN (?, ?, ?) GROUP BY d.name, d.comment ORDER BY d.name",
            _SYSTEM_DATABASES,
            timeout_s=self.timeout_s,
        )
        return Page(
            [
                Schema(None, r["name"], blank_to_none(r["comment"]), int(r["table_count"]))
                for r in rows
            ]
        )

    def list_tables(
        self, schema: Schema, *, page_token: str | None, changed_since: datetime | None
    ) -> Page[Table]:
        since = None
        if changed_since is not None:
            since = changed_since.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        rows = self._ex.run(
            "SELECT name, engine, comment, total_rows, total_bytes, "
            "toTimeZone(metadata_modification_time, 'UTC') AS modified_at, "
            "partition_key, sorting_key "
            "FROM system.tables "
            "WHERE database = ? AND NOT is_temporary "
            "AND (? IS NULL OR name > ?) "
            "AND (? IS NULL OR metadata_modification_time > toDateTime(?, 'UTC')) "
            "ORDER BY name LIMIT ?",
            (schema.name, page_token, page_token, since, since, self.page_size + 1),
            timeout_s=self.timeout_s,
        )
        more = len(rows) > self.page_size
        rows = rows[: self.page_size]
        items = [
            Table(
                ObjectRef(None, schema.name, r["name"]),
                _kind(r["engine"]),
                blank_to_none(r["comment"]),
                None if r["total_rows"] is None else int(r["total_rows"]),
                None if r["total_bytes"] is None else int(r["total_bytes"]),
                None,
                utc(r["modified_at"]),
                blank_to_none(r["partition_key"]),
                _sorting_columns(r["sorting_key"]),
            )
            for r in rows
        ]
        return Page(items, rows[-1]["name"] if more else None)

    def columns(self, tables: Sequence[ObjectRef]) -> Mapping[ObjectRef, list[Column]]:
        out: dict[ObjectRef, list[Column]] = {t.table_ref(): [] for t in tables}
        if not tables:
            return out
        clause, params = _pairs(tables)
        rows = self._ex.run(
            "SELECT database AS schema_name, `table` AS table_name, name AS column_name, "
            "position AS ordinal, type AS data_type, default_kind, default_expression, "
            "comment "
            f"FROM system.columns WHERE {clause} ORDER BY database, `table`, position",
            params,
            timeout_s=self.timeout_s,
        )
        for r in rows:
            tref = ObjectRef(None, r["schema_name"], r["table_name"])
            inner, nullable = _unwrap(r["data_type"])
            default = r["default_expression"] if r["default_kind"] == "DEFAULT" else None
            out.setdefault(tref, []).append(
                Column(
                    ObjectRef(None, r["schema_name"], r["table_name"], r["column_name"]),
                    int(r["ordinal"]),
                    r["data_type"],
                    _family(inner),
                    nullable,
                    blank_to_none(r["comment"]),
                    blank_to_none(default),
                )
            )
        return out

    def constraints(self, tables: Sequence[ObjectRef]) -> Mapping[ObjectRef, list[Constraint]]:
        return {t.table_ref(): [] for t in tables}

    def view_definitions(self, tables: Sequence[ObjectRef]) -> Mapping[ObjectRef, str]:
        """``system.tables.as_select`` (the view's SELECT; empty for a table, so absent)."""
        out: dict[ObjectRef, str] = {}
        if not tables:
            return out
        clause, params = _pairs(tables)
        rows = self._ex.run(
            "SELECT database AS schema_name, name AS table_name, as_select "
            f"FROM system.tables WHERE ({clause}) "
            "AND engine IN ('View', 'MaterializedView', 'LiveView', 'WindowView') "
            "ORDER BY database, name",
            params,
            timeout_s=self.timeout_s,
        )
        for r in rows:
            text = definition_text(r["as_select"])
            if text is not None:
                out[ObjectRef(None, r["schema_name"], r["table_name"])] = text
        return out

    def capabilities(self) -> ReaderCapabilities:
        return ReaderCapabilities(
            comments=True, stats=True, last_altered=True, incremental=True, view_definitions=True
        )


register_reader("clickhouse", ClickHouseReader)
