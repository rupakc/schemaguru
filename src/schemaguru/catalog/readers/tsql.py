"""SQL Server / Azure Synapse reader over ``sys.*`` catalog views (spec 6.2).

Tables and views come from ``sys.objects`` (type ``U``/``V``), comments from the ``MS_Description``
extended property, keys from ``sys.key_constraints`` / ``sys.foreign_keys``. A database is the
connection's scope, so refs carry no catalog (``list_catalogs`` yields nothing). ``sys.objects``
dates are server-local with no zone, so ``created_at``/``last_altered`` are reported as unknown
rather than guessed. Synapse dedicated pools declare keys ``NOT ENFORCED`` (engine edition 6);
the reader reads that from ``SERVERPROPERTY`` and reports ``enforced=False`` there.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime

from schemaguru.catalog.executor import MetadataExecutor
from schemaguru.catalog.protocol import Page, ReaderCapabilities
from schemaguru.catalog.readers._rules import blank_to_none, compile_rules, family_by
from schemaguru.catalog.registry import register_reader
from schemaguru.model.refs import ObjectRef
from schemaguru.model.structure import Column, Constraint, ConstraintKind, Schema, Table, TableKind

_SYSTEM_SCHEMAS = ("sys", "INFORMATION_SCHEMA", "guest")
_KINDS: Mapping[str, TableKind] = {"U": "table", "V": "view"}
_CONSTRAINT_KINDS: Mapping[str, ConstraintKind] = {"PK": "primary_key", "UQ": "unique"}
_RULES = compile_rules(
    (
        (r"^(tinyint|smallint|int|bigint)$", "integer"),
        (r"^(decimal|numeric|money|smallmoney)$", "decimal"),
        (r"^(float|real)$", "float"),
        (r"^bit$", "boolean"),
        (r"^(char|varchar|nchar|nvarchar|text|ntext|uniqueidentifier)$", "string"),
        (r"^date$", "date"),
        (r"^(datetime|datetime2|smalldatetime|datetimeoffset)$", "timestamp"),
        (r"^time$", "time"),
        (r"^(binary|varbinary|image|timestamp)$", "bytes"),
    )
)
_ENFORCED = "CASE WHEN CAST(SERVERPROPERTY('EngineEdition') AS int) = 6 THEN 0 ELSE 1 END"
_COMMENT = (
    "CAST((SELECT TOP 1 ep.value FROM sys.extended_properties ep "
    "WHERE ep.class = {cls} AND ep.major_id = {major} AND ep.minor_id = {minor} "
    "AND ep.name = 'MS_Description') AS nvarchar(4000))"
)


def _native_type(name: str, max_length: int, precision: int, scale: int) -> str:
    """Re-assemble the declared type from ``sys.types`` + ``sys.columns`` facts."""
    if name in ("char", "varchar", "binary", "varbinary"):
        return f"{name}({'max' if max_length == -1 else max_length})"
    if name in ("nchar", "nvarchar"):
        return f"{name}({'max' if max_length == -1 else max_length // 2})"
    if name in ("decimal", "numeric"):
        return f"{name}({precision},{scale})"
    if name in ("datetime2", "datetimeoffset", "time"):
        return f"{name}({scale})"
    return name


def _pairs(tables: Sequence[ObjectRef]) -> tuple[str, list[str]]:
    clause = " OR ".join("(s.name = ? AND o.name = ?)" for _ in tables)
    params: list[str] = []
    for t in tables:
        params += [t.schema or "dbo", t.table]
    return clause, params


class TSQLReader:
    dialect = "tsql"

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
        rows = self._ex.run(
            "SELECT s.name AS name, "
            f"{_COMMENT.format(cls=3, major='s.schema_id', minor=0)} AS comment, "
            "(SELECT COUNT(*) FROM sys.objects o WHERE o.schema_id = s.schema_id "
            " AND o.type IN ('U', 'V') AND o.is_ms_shipped = 0) AS table_count "
            "FROM sys.schemas s "
            "WHERE s.name NOT IN (?, ?, ?) AND s.schema_id NOT BETWEEN 16384 AND 16399 "
            "ORDER BY s.name COLLATE Latin1_General_BIN2",
            _SYSTEM_SCHEMAS,
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
        rows = self._ex.run(
            "SELECT TOP (?) o.name AS name, o.type AS obj_type, "
            f"{_COMMENT.format(cls=1, major='o.object_id', minor=0)} AS comment, "
            "CASE WHEN o.type = 'U' THEN (SELECT SUM(p.rows) FROM sys.partitions p "
            " WHERE p.object_id = o.object_id AND p.index_id < 2) END AS row_count "
            "FROM sys.objects o JOIN sys.schemas s ON s.schema_id = o.schema_id "
            "WHERE s.name = ? AND o.type IN ('U', 'V') AND o.is_ms_shipped = 0 "
            "AND (CAST(? AS nvarchar(256)) IS NULL "
            " OR o.name COLLATE Latin1_General_BIN2 > CAST(? AS nvarchar(256)) "
            " COLLATE Latin1_General_BIN2) "
            "ORDER BY o.name COLLATE Latin1_General_BIN2",
            (self.page_size + 1, schema.name, page_token, page_token),
            timeout_s=self.timeout_s,
        )
        more = len(rows) > self.page_size
        rows = rows[: self.page_size]
        items = [
            Table(
                ObjectRef(None, schema.name, r["name"]),
                _KINDS[r["obj_type"].strip()],
                blank_to_none(r["comment"]),
                None if r["row_count"] is None else int(r["row_count"]),
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
            "SELECT s.name AS schema_name, o.name AS table_name, o.type AS obj_type, "
            "c.column_id AS ordinal, c.name AS column_name, ty.name AS type_name, "
            "ty.is_user_defined AS is_user_defined, bt.name AS base_name, "
            "c.max_length AS max_length, c.precision AS prec, c.scale AS scale, "
            "c.is_nullable AS is_nullable, dc.definition AS column_default, "
            f"{_COMMENT.format(cls=1, major='o.object_id', minor='c.column_id')} AS comment "
            "FROM sys.columns c JOIN sys.objects o ON o.object_id = c.object_id "
            "JOIN sys.schemas s ON s.schema_id = o.schema_id "
            "JOIN sys.types ty ON ty.user_type_id = c.user_type_id "
            "LEFT JOIN sys.types bt ON bt.user_type_id = ty.system_type_id "
            "LEFT JOIN sys.default_constraints dc ON dc.parent_object_id = c.object_id "
            " AND dc.parent_column_id = c.column_id "
            f"WHERE o.type IN ('U', 'V') AND ({clause}) "
            "ORDER BY s.name, o.name, c.column_id",
            params,
            timeout_s=self.timeout_s,
        )
        for r in rows:
            tref = ObjectRef(None, r["schema_name"], r["table_name"])
            native = (
                r["type_name"]
                if r["is_user_defined"]
                else _native_type(
                    r["type_name"], int(r["max_length"]), int(r["prec"]), int(r["scale"])
                )
            )
            nullable = None if r["obj_type"].strip() == "V" else bool(r["is_nullable"])
            out.setdefault(tref, []).append(
                Column(
                    ObjectRef(None, r["schema_name"], r["table_name"], r["column_name"]),
                    int(r["ordinal"]),
                    native,
                    family_by(_RULES, r["base_name"] or r["type_name"]),
                    nullable,
                    blank_to_none(r["comment"]),
                    r["column_default"],
                )
            )
        return out

    def constraints(self, tables: Sequence[ObjectRef]) -> Mapping[ObjectRef, list[Constraint]]:
        out: dict[ObjectRef, list[Constraint]] = {t.table_ref(): [] for t in tables}
        if not tables:
            return out
        clause, params = _pairs(tables)
        keys = self._ex.run(
            "SELECT s.name AS schema_name, o.name AS table_name, kc.type AS ctype, "
            "kc.name AS cname, ic.key_ordinal AS pos, c.name AS column_name, "
            f"{_ENFORCED} AS enforced "
            "FROM sys.key_constraints kc JOIN sys.objects o ON o.object_id = kc.parent_object_id "
            "JOIN sys.schemas s ON s.schema_id = o.schema_id "
            "JOIN sys.index_columns ic ON ic.object_id = kc.parent_object_id "
            " AND ic.index_id = kc.unique_index_id AND ic.is_included_column = 0 "
            "JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id "
            f"WHERE kc.type IN ('PK', 'UQ') AND ({clause}) "
            "ORDER BY s.name, o.name, kc.type DESC, kc.name, ic.key_ordinal",
            params,
            timeout_s=self.timeout_s,
        )
        grouped: dict[tuple[str, str, str, str], list[Mapping[str, str]]] = {}
        for r in keys:
            grouped.setdefault(
                (r["schema_name"], r["table_name"], r["ctype"].strip(), r["cname"]), []
            ).append(r)
        for (schema, table, ctype, _name), parts in grouped.items():
            tref = ObjectRef(None, schema, table)
            out.setdefault(tref, []).append(
                Constraint(
                    _CONSTRAINT_KINDS[ctype],
                    tref,
                    tuple(p["column_name"] for p in parts),
                    enforced=bool(parts[0]["enforced"]),
                )
            )
        fks = self._ex.run(
            "SELECT s.name AS schema_name, o.name AS table_name, fk.name AS cname, "
            "fkc.constraint_column_id AS pos, pc.name AS column_name, "
            "rs.name AS ref_schema, ro.name AS ref_table, rc.name AS ref_column, "
            f"CASE WHEN fk.is_disabled = 1 THEN 0 ELSE {_ENFORCED} END AS enforced "
            "FROM sys.foreign_keys fk JOIN sys.objects o ON o.object_id = fk.parent_object_id "
            "JOIN sys.schemas s ON s.schema_id = o.schema_id "
            "JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id "
            "JOIN sys.columns pc ON pc.object_id = fkc.parent_object_id "
            " AND pc.column_id = fkc.parent_column_id "
            "JOIN sys.objects ro ON ro.object_id = fkc.referenced_object_id "
            "JOIN sys.schemas rs ON rs.schema_id = ro.schema_id "
            "JOIN sys.columns rc ON rc.object_id = fkc.referenced_object_id "
            " AND rc.column_id = fkc.referenced_column_id "
            f"WHERE ({clause}) ORDER BY s.name, o.name, fk.name, fkc.constraint_column_id",
            params,
            timeout_s=self.timeout_s,
        )
        fgrouped: dict[tuple[str, str, str], list[Mapping[str, str]]] = {}
        for r in fks:
            fgrouped.setdefault((r["schema_name"], r["table_name"], r["cname"]), []).append(r)
        for (schema, table, _name), parts in fgrouped.items():
            tref = ObjectRef(None, schema, table)
            out.setdefault(tref, []).append(
                Constraint(
                    "foreign_key",
                    tref,
                    tuple(p["column_name"] for p in parts),
                    ObjectRef(None, parts[0]["ref_schema"], parts[0]["ref_table"]),
                    tuple(p["ref_column"] for p in parts),
                    bool(parts[0]["enforced"]),
                )
            )
        return out

    def capabilities(self) -> ReaderCapabilities:
        return ReaderCapabilities(comments=True, stats=True, constraints=True)


register_reader("tsql", TSQLReader)
