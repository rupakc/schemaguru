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
from schemaguru.diagnostics import Diagnostic, Severity
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
        self.diagnostics: list[Diagnostic] = []

    def list_catalogs(self) -> Iterator[str]:
        return iter(())

    def list_schemas(self, catalog: str | None, *, page_token: str | None) -> Page[Schema]:
        if page_token is not None:  # one page holds every schema: a later page is empty
            return Page([], None)
        rows = self._ex.run(
            "SELECT n.nspname AS name, obj_description(n.oid, 'pg_namespace') AS comment, "
            "(SELECT count(*) FROM pg_class c WHERE c.relnamespace = n.oid "
            " AND c.relkind IN ('r', 'p', 'v', 'm', 'f') AND NOT c.relispartition) AS table_count "
            "FROM pg_namespace n "
            # a prefix test, not LIKE 'pg\\_%': FactLang's metadata guard refuses any backslash
            "WHERE n.nspname <> 'information_schema' AND left(n.nspname, 3) <> 'pg_' "
            "ORDER BY n.nspname",
            timeout_s=self.timeout_s,
        )
        return Page([Schema(None, r["name"], r["comment"], int(r["table_count"])) for r in rows])

    def list_tables(
        self, schema: Schema, *, page_token: str | None, changed_since: datetime | None
    ) -> Page[Table]:
        rows = self._ex.run(
            "SELECT c.relname AS name, c.relkind AS relkind, "
            "CASE WHEN c.relkind IN ('r', 'm') AND NOT (c.relpages = 0 AND c.reltuples = 0) "
            " THEN c.reltuples END AS reltuples, "
            "CASE WHEN c.relkind IN ('r', 'm') THEN pg_total_relation_size(c.oid) END "
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
            timeout_s=self.timeout_s,
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
            timeout_s=self.timeout_s,
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
            "con.convalidated AS validated, fn.nspname AS ref_schema, fc.relname AS ref_table, "
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
            timeout_s=self.timeout_s,
        )
        for r in rows:
            kind = _CONSTRAINT_KINDS[r["contype"]]
            tref = ObjectRef(None, r["schema_name"], r["table_name"])
            ref_table = ref_cols = None
            enforced = True
            if kind == "foreign_key":
                ref_table = ObjectRef(None, r["ref_schema"], r["ref_table"])
                ref_cols = tuple(r["ref_cols"])
                # NOT VALID: new rows are checked, existing rows were never verified
                enforced = bool(r["validated"])
            out.setdefault(tref, []).append(
                Constraint(kind, tref, tuple(r["cols"]), ref_table, ref_cols, enforced)
            )
        try:
            self._unique_indexes(tables, out)
        except Exception as e:
            self.diagnostics.append(
                Diagnostic(
                    "SG102",
                    Severity.WARNING,
                    f"unique indexes not read ({type(e).__name__}); they need PostgreSQL 11+",
                    hint="primary, unique and foreign key constraints are still reported",
                )
            )
        return out

    def _unique_indexes(
        self, tables: Sequence[ObjectRef], out: dict[ObjectRef, list[Constraint]]
    ) -> None:
        """A unique index that backs no constraint still guarantees uniqueness (the table has
        ``CREATE UNIQUE INDEX`` and no ``UNIQUE``). Partial, expression and invalid indexes
        guarantee nothing about the whole column list and are skipped."""
        clause, params = _pairs(tables, "n.nspname", "c.relname")
        rows = self._ex.run(
            "SELECT n.nspname AS schema_name, c.relname AS table_name, "
            "(SELECT array_agg(a.attname::text ORDER BY k.ord) "
            " FROM unnest(i.indkey::int2[]) WITH ORDINALITY AS k(attnum, ord) "
            " JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum "
            " WHERE k.ord <= i.indnkeyatts) AS cols "
            "FROM pg_index i JOIN pg_class c ON c.oid = i.indrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE i.indisunique AND NOT i.indisprimary AND i.indisvalid "
            "AND i.indpred IS NULL AND i.indexprs IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM pg_constraint con WHERE con.conindid = i.indexrelid) "
            f"AND ({clause}) "
            "ORDER BY n.nspname, c.relname, i.indexrelid",
            params,
            timeout_s=self.timeout_s,
        )
        for r in rows:
            tref = ObjectRef(None, r["schema_name"], r["table_name"])
            out.setdefault(tref, []).append(
                Constraint("unique", tref, tuple(r["cols"]), None, None, True)
            )

    def capabilities(self) -> ReaderCapabilities:
        return ReaderCapabilities(comments=True, stats=True, constraints=True)


register_reader("postgres", PostgresReader)
