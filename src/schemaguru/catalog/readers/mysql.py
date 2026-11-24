"""MySQL / MariaDB reader over ``information_schema`` (spec 6.2).

A MySQL *schema* is a database, so refs carry no catalog (``table_catalog`` is always ``def``) and
the reader works in the server the connection points at. Every selected column is aliased because
MySQL 8 returns ``information_schema`` column names in upper case. ``TABLE_ROWS`` is an engine
estimate that may be cached for ``information_schema_stats_expiry`` seconds, and is NULL for views.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime, timezone

from schemaguru.catalog.executor import MetadataExecutor
from schemaguru.catalog.protocol import Page, ReaderCapabilities
from schemaguru.catalog.readers._rules import blank_to_none, compile_rules, family_by, utc
from schemaguru.catalog.readers._types import family_of
from schemaguru.catalog.registry import register_reader
from schemaguru.model.refs import ObjectRef
from schemaguru.model.structure import Column, Constraint, ConstraintKind, Schema, Table, TableKind

_SYSTEM_SCHEMAS = ("information_schema", "mysql", "performance_schema", "sys")
_KINDS: Mapping[str, TableKind] = {"BASE TABLE": "table", "VIEW": "view"}
_CONSTRAINT_KINDS: Mapping[str, ConstraintKind] = {
    "PRIMARY KEY": "primary_key",
    "UNIQUE": "unique",
    "FOREIGN KEY": "foreign_key",
}
# Families the shared table does not know (MySQL-only spellings); consulted first.
_RULES = compile_rules(
    (
        (r"^(mediumint|year)\b", "integer"),
        (r"^(tinytext|mediumtext|longtext|set)\b", "string"),
        (r"^(tinyblob|mediumblob|longblob)\b", "bytes"),
        (r"^(bit|geometry|point|linestring|polygon|multi\w+|geometrycollection)\b", "other"),
    )
)
# information_schema timestamps are naive in the session time zone; convert in SQL, label as UTC.
_UTC = "CONVERT_TZ({col}, @@session.time_zone, '+00:00')"


def _family(native_type: str) -> str:
    local = family_by(_RULES, native_type)
    return local if local != "other" else family_of(native_type)


def _pairs(tables: Sequence[ObjectRef], schema_col: str, table_col: str) -> tuple[str, list[str]]:
    clause = " OR ".join(f"({schema_col} = ? AND {table_col} = ?)" for _ in tables)
    params: list[str] = []
    for t in tables:
        params += [t.schema or "", t.table]
    return clause, params


def _default(raw: str | None, mariadb: bool) -> str | None:
    """MySQL reports a default as its raw text; MariaDB reports SQL NULL as the word ``NULL`` and
    quotes string literals. Normalise MariaDB to MySQL's form."""
    if raw is None or not mariadb:
        return raw
    if raw == "NULL":
        return None
    if len(raw) >= 2 and raw[0] == raw[-1] == "'":
        return raw[1:-1].replace("''", "'")
    return raw


def _int(value: object) -> int | None:
    return None if value is None else int(str(value))


class MySQLReader:
    dialect = "mysql"

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
            "SELECT s.schema_name AS name, "
            "(SELECT COUNT(*) FROM information_schema.tables t "
            " WHERE t.table_schema = s.schema_name AND t.table_type IN ('BASE TABLE', 'VIEW')) "
            " AS table_count "
            "FROM information_schema.schemata s "
            "WHERE s.schema_name NOT IN (?, ?, ?, ?) ORDER BY s.schema_name",
            _SYSTEM_SCHEMAS,
            timeout_s=self.timeout_s,
        )
        return Page([Schema(None, r["name"], None, int(r["table_count"])) for r in rows])

    def list_tables(
        self, schema: Schema, *, page_token: str | None, changed_since: datetime | None
    ) -> Page[Table]:
        since = None
        if changed_since is not None:
            since = changed_since.astimezone(timezone.utc).replace(tzinfo=None)
        rows = self._ex.run(
            "SELECT t.table_name AS name, t.table_type AS table_type, "
            "t.table_comment AS comment, t.table_rows AS table_rows, "
            "t.data_length AS data_length, t.index_length AS index_length, "
            f"{_UTC.format(col='t.create_time')} AS created_at, "
            f"{_UTC.format(col='t.update_time')} AS updated_at "
            "FROM information_schema.tables t "
            "WHERE t.table_schema = ? AND t.table_type IN ('BASE TABLE', 'VIEW') "
            "AND (? IS NULL OR BINARY t.table_name > BINARY ?) "
            "AND (? IS NULL OR t.update_time IS NULL "
            f" OR {_UTC.format(col='t.update_time')} > ?) "
            "ORDER BY BINARY t.table_name LIMIT ?",
            (schema.name, page_token, page_token, since, since, self.page_size + 1),
            timeout_s=self.timeout_s,
        )
        more = len(rows) > self.page_size
        rows = rows[: self.page_size]
        items: list[Table] = []
        for r in rows:
            kind = _KINDS[r["table_type"]]
            is_view = kind == "view"
            size = None
            if not is_view and r["data_length"] is not None:
                size = int(r["data_length"]) + int(r["index_length"] or 0)
            items.append(
                Table(
                    ObjectRef(None, schema.name, r["name"]),
                    kind,
                    None if is_view else blank_to_none(r["comment"]),
                    None if is_view else _int(r["table_rows"]),
                    size,
                    utc(r["created_at"]),
                    utc(r["updated_at"]),
                )
            )
        return Page(items, rows[-1]["name"] if more else None)

    def columns(self, tables: Sequence[ObjectRef]) -> Mapping[ObjectRef, list[Column]]:
        out: dict[ObjectRef, list[Column]] = {t.table_ref(): [] for t in tables}
        if not tables:
            return out
        clause, params = _pairs(tables, "c.table_schema", "c.table_name")
        rows = self._ex.run(
            "SELECT c.table_schema AS schema_name, c.table_name AS table_name, "
            "t.table_type AS table_type, c.column_name AS column_name, "
            "c.ordinal_position AS ordinal, c.column_type AS data_type, "
            "c.is_nullable AS is_nullable, c.column_default AS column_default, "
            "c.column_comment AS comment, "
            "LOCATE('MariaDB', @@version) > 0 AS is_mariadb "
            "FROM information_schema.columns c JOIN information_schema.tables t "
            "ON t.table_schema = c.table_schema AND t.table_name = c.table_name "
            f"WHERE {clause} ORDER BY c.table_schema, c.table_name, c.ordinal_position",
            params,
            timeout_s=self.timeout_s,
        )
        for r in rows:
            tref = ObjectRef(None, r["schema_name"], r["table_name"])
            nullable = None if r["table_type"] == "VIEW" else r["is_nullable"] == "YES"
            out.setdefault(tref, []).append(
                Column(
                    ObjectRef(None, r["schema_name"], r["table_name"], r["column_name"]),
                    int(r["ordinal"]),
                    r["data_type"],
                    _family(r["data_type"]),
                    nullable,
                    blank_to_none(r["comment"]),
                    _default(r["column_default"], bool(r["is_mariadb"])),
                )
            )
        return out

    def constraints(self, tables: Sequence[ObjectRef]) -> Mapping[ObjectRef, list[Constraint]]:
        out: dict[ObjectRef, list[Constraint]] = {t.table_ref(): [] for t in tables}
        if not tables:
            return out
        clause, params = _pairs(tables, "tc.table_schema", "tc.table_name")
        rows = self._ex.run(
            "SELECT tc.table_schema AS schema_name, tc.table_name AS table_name, "
            "tc.constraint_type AS ctype, tc.constraint_name AS cname, "
            "k.ordinal_position AS pos, k.column_name AS column_name, "
            "k.referenced_table_schema AS ref_schema, k.referenced_table_name AS ref_table, "
            "k.referenced_column_name AS ref_column "
            "FROM information_schema.table_constraints tc "
            "JOIN information_schema.key_column_usage k "
            "ON k.constraint_schema = tc.constraint_schema "
            "AND k.constraint_name = tc.constraint_name "
            "AND k.table_schema = tc.table_schema AND k.table_name = tc.table_name "
            f"WHERE tc.constraint_type IN ('PRIMARY KEY', 'UNIQUE', 'FOREIGN KEY') AND ({clause}) "
            "ORDER BY tc.table_schema, tc.table_name, tc.constraint_type DESC, "
            "tc.constraint_name, k.ordinal_position",
            params,
            timeout_s=self.timeout_s,
        )
        grouped: dict[tuple[str, str, str, str], list[Mapping[str, str]]] = {}
        for r in rows:
            grouped.setdefault(
                (r["schema_name"], r["table_name"], r["ctype"], r["cname"]), []
            ).append(r)
        for (schema, table, ctype, _name), parts in grouped.items():
            kind = _CONSTRAINT_KINDS[ctype]
            tref = ObjectRef(None, schema, table)
            ref_table = ref_cols = None
            if kind == "foreign_key":
                ref_table = ObjectRef(None, parts[0]["ref_schema"], parts[0]["ref_table"])
                ref_cols = tuple(p["ref_column"] for p in parts)
            out.setdefault(tref, []).append(
                Constraint(
                    kind, tref, tuple(p["column_name"] for p in parts), ref_table, ref_cols, True
                )
            )
        return out

    def capabilities(self) -> ReaderCapabilities:
        return ReaderCapabilities(
            comments=True, stats=True, last_altered=True, constraints=True, incremental=True
        )


register_reader("mysql", MySQLReader)
