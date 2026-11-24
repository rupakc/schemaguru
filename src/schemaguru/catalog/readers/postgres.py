"""PostgreSQL reader over ``pg_catalog`` (not ``information_schema``, which hides objects the
role cannot use and is slow at scale): ``pg_class`` ``reltuples`` row estimates and
``pg_description`` comments. Refs carry no catalog (one database per connection)."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime

from schemaguru.catalog.executor import MetadataExecutor
from schemaguru.catalog.protocol import Page, ReaderCapabilities
from schemaguru.catalog.readers._types import family_of
from schemaguru.catalog.registry import register_reader
from schemaguru.model.refs import ObjectRef
from schemaguru.model.structure import Column, Constraint, ConstraintKind, Schema, Table, TableKind

_KINDS: Mapping[str, TableKind] = {
    "r": "table",
    "p": "table",
    "v": "view",
    "m": "materialized_view",
    "f": "external",
}
_CONSTRAINT_KINDS: Mapping[str, ConstraintKind] = {
    "p": "primary_key",
    "u": "unique",
    "f": "foreign_key",
}


def _pairs(tables: Sequence[ObjectRef], schema_col: str, table_col: str) -> tuple[str, list[str]]:
    clause = " OR ".join(f"({schema_col} = ? AND {table_col} = ?)" for _ in tables)
    params: list[str] = []
    for t in tables:
        params += [t.schema or "public", t.table]
    return clause, params


class PostgresReader:
    dialect = "postgres"

    def __init__(self, executor: MetadataExecutor, *, page_size: int = 1000) -> None:
        self._ex = executor
        self.page_size = page_size

    def list_catalogs(self) -> Iterator[str]:
        return iter(())

    def list_schemas(self, catalog: str | None, *, page_token: str | None) -> Page[Schema]:
        rows = self._ex.run(
            "SELECT n.nspname AS name, obj_description(n.oid, 'pg_namespace') AS comment, "
            "(SELECT count(*) FROM pg_class c WHERE c.relnamespace = n.oid "
            " AND c.relkind IN ('r', 'p', 'v', 'm', 'f')) AS table_count "
            "FROM pg_namespace n "
            "WHERE n.nspname <> 'information_schema' AND n.nspname NOT LIKE 'pg\\_%' "
            "ORDER BY n.nspname"
        )
        return Page([Schema(None, r["name"], r["comment"], int(r["table_count"])) for r in rows])

    def list_tables(
        self, schema: Schema, *, page_token: str | None, changed_since: datetime | None
    ) -> Page[Table]:
        rows = self._ex.run(
            "SELECT c.relname AS name, c.relkind AS relkind, c.reltuples AS reltuples, "
            "CASE WHEN c.relkind IN ('r', 'p', 'm') THEN pg_total_relation_size(c.oid) END "
            " AS size_bytes, "
            "CASE WHEN c.relkind = 'p' THEN pg_get_partkeydef(c.oid) END AS partitioning, "
            "d.description AS comment "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "LEFT JOIN pg_description d ON d.objoid = c.oid AND d.objsubid = 0 "
            " AND d.classoid = CAST('pg_class' AS regclass) "
            "WHERE n.nspname = ? AND c.relkind IN ('r', 'p', 'v', 'm', 'f') "
            "AND c.relispartition = false "
            'AND (CAST(? AS text) IS NULL OR c.relname COLLATE "C" > CAST(? AS text)) '
            'ORDER BY c.relname COLLATE "C" LIMIT ?',
            (schema.name, page_token, page_token, self.page_size + 1),
        )
        more = len(rows) > self.page_size
        rows = rows[: self.page_size]
        items: list[Table] = []
        for r in rows:
            tuples = r["reltuples"]
            items.append(
                Table(
                    ObjectRef(None, schema.name, r["name"]),
                    _KINDS[r["relkind"]],
                    r["comment"],
                    None if tuples is None or tuples < 0 else int(tuples),
                    None if r["size_bytes"] is None else int(r["size_bytes"]),
                    partitioning=r["partitioning"],
                )
            )
        return Page(items, rows[-1]["name"] if more else None)

    def columns(self, tables: Sequence[ObjectRef]) -> Mapping[ObjectRef, list[Column]]:
        out: dict[ObjectRef, list[Column]] = {t.table_ref(): [] for t in tables}
        if not tables:
            return out
        clause, params = _pairs(tables, "n.nspname", "c.relname")
        rows = self._ex.run(
            "SELECT n.nspname AS schema_name, c.relname AS table_name, c.relkind AS relkind, "
            "a.attnum AS ordinal, a.attname AS column_name, "
            "format_type(a.atttypid, a.atttypmod) AS data_type, a.attnotnull AS not_null, "
            "pg_get_expr(ad.adbin, ad.adrelid) AS column_default, "
            "col_description(c.oid, a.attnum) AS comment "
            "FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "LEFT JOIN pg_attrdef ad ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum "
            f"WHERE a.attnum > 0 AND NOT a.attisdropped AND ({clause}) "
            "ORDER BY n.nspname, c.relname, a.attnum",
            params,
        )
        for r in rows:
            tref = ObjectRef(None, r["schema_name"], r["table_name"])
            nullable = None if r["relkind"] in ("v", "m") else not r["not_null"]
            out.setdefault(tref, []).append(
                Column(
                    ObjectRef(None, r["schema_name"], r["table_name"], r["column_name"]),
                    int(r["ordinal"]),
                    r["data_type"],
                    family_of(r["data_type"]),
                    nullable,
                    r["comment"],
                    r["column_default"],
                )
            )
        return out

    def constraints(self, tables: Sequence[ObjectRef]) -> Mapping[ObjectRef, list[Constraint]]:
        out: dict[ObjectRef, list[Constraint]] = {t.table_ref(): [] for t in tables}
        if not tables:
            return out
        clause, params = _pairs(tables, "n.nspname", "c.relname")
        rows = self._ex.run(
            "SELECT n.nspname AS schema_name, c.relname AS table_name, con.contype AS contype, "
            "(SELECT array_agg(a.attname::text ORDER BY k.ord) "
            " FROM unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord) "
            " JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum) AS cols, "
            "fn.nspname AS ref_schema, fc.relname AS ref_table, "
            "(SELECT array_agg(a.attname::text ORDER BY k.ord) "
            " FROM unnest(con.confkey) WITH ORDINALITY AS k(attnum, ord) "
            " JOIN pg_attribute a ON a.attrelid = con.confrelid AND a.attnum = k.attnum) "
            " AS ref_cols "
            "FROM pg_constraint con JOIN pg_class c ON c.oid = con.conrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "LEFT JOIN pg_class fc ON fc.oid = con.confrelid "
            "LEFT JOIN pg_namespace fn ON fn.oid = fc.relnamespace "
            f"WHERE con.contype IN ('p', 'u', 'f') AND ({clause}) "
            "ORDER BY n.nspname, c.relname, con.contype DESC, con.conname",
            params,
        )
        for r in rows:
            kind = _CONSTRAINT_KINDS[r["contype"]]
            tref = ObjectRef(None, r["schema_name"], r["table_name"])
            ref_table = ref_cols = None
            if kind == "foreign_key":
                ref_table = ObjectRef(None, r["ref_schema"], r["ref_table"])
                ref_cols = tuple(r["ref_cols"])
            out.setdefault(tref, []).append(
                Constraint(kind, tref, tuple(r["cols"]), ref_table, ref_cols, True)
            )
        return out

    def capabilities(self) -> ReaderCapabilities:
        return ReaderCapabilities(comments=True, stats=True, constraints=True)


register_reader("postgres", PostgresReader)
