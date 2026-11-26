"""Trino reader over ``<catalog>.information_schema`` (spec 6.2).

Trino is federated: every connector catalog is a ``catalog`` (``list_catalogs`` runs ``SHOW
CATALOGS``; ``system`` is skipped) and refs carry it. A catalog name cannot be a bind parameter,
so it is quoted as an identifier; every value is bound. Trino exposes no constraints, row counts
or sizes in its metadata (``SHOW STATS`` is per-table and connector-dependent), so those are
unknown, and ``constraints()`` returns nothing. Table comments come from
``system.metadata.table_comments``; column comments from ``information_schema.columns``.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime

from schemaguru.catalog.executor import MetadataExecutor
from schemaguru.catalog.protocol import Page, ReaderCapabilities
from schemaguru.catalog.readers._common import definition_text
from schemaguru.catalog.readers._rules import blank_to_none
from schemaguru.catalog.readers._types import family_of
from schemaguru.catalog.registry import register_reader
from schemaguru.model.refs import ObjectRef
from schemaguru.model.structure import Column, Constraint, Schema, Table, TableKind

_SKIP_CATALOGS = frozenset({"system"})
_KINDS: Mapping[str, TableKind] = {"BASE TABLE": "table", "VIEW": "view"}


def _q(identifier: str) -> str:
    """Quote an identifier (catalogs cannot be bound)."""
    return '"' + identifier.replace('"', '""') + '"'


class TrinoReader:
    dialect = "trino"

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
        rows = self._ex.run("SHOW CATALOGS", timeout_s=self.timeout_s)
        names = sorted(str(next(iter(r.values()))) for r in rows)
        return iter([n for n in names if n not in _SKIP_CATALOGS])

    def list_schemas(self, catalog: str | None, *, page_token: str | None) -> Page[Schema]:
        if page_token is not None:  # one page holds every schema: a later page is empty
            return Page([], None)
        if catalog is None:
            return Page([])
        rows = self._ex.run(
            f"SELECT schema_name FROM {_q(catalog)}.information_schema.schemata "
            "WHERE schema_name <> ? ORDER BY schema_name",
            ("information_schema",),
            timeout_s=self.timeout_s,
        )
        return Page([Schema(catalog, r["schema_name"]) for r in rows])

    def list_tables(
        self, schema: Schema, *, page_token: str | None, changed_since: datetime | None
    ) -> Page[Table]:
        if schema.catalog is None:
            return Page([])
        rows = self._ex.run(
            "SELECT t.table_name AS name, t.table_type AS table_type, c.comment AS comment "
            f"FROM {_q(schema.catalog)}.information_schema.tables t "
            "LEFT JOIN system.metadata.table_comments c "
            "ON c.catalog_name = ? AND c.schema_name = ? AND c.table_name = t.table_name "
            "WHERE t.table_schema = ? AND t.table_type IN ('BASE TABLE', 'VIEW') "
            "AND (CAST(? AS varchar) IS NULL OR t.table_name > CAST(? AS varchar)) "
            "ORDER BY t.table_name LIMIT ?",
            (schema.catalog, schema.name, schema.name, page_token, page_token, self.page_size + 1),
            timeout_s=self.timeout_s,
        )
        more = len(rows) > self.page_size
        rows = rows[: self.page_size]
        items = [
            Table(
                ObjectRef(schema.catalog, schema.name, r["name"]),
                _KINDS[r["table_type"]],
                blank_to_none(r["comment"]),
            )
            for r in rows
        ]
        return Page(items, rows[-1]["name"] if more else None)

    def columns(self, tables: Sequence[ObjectRef]) -> Mapping[ObjectRef, list[Column]]:
        out: dict[ObjectRef, list[Column]] = {t.table_ref(): [] for t in tables}
        by_catalog: dict[str, list[ObjectRef]] = {}
        for t in tables:
            if t.catalog is not None:
                by_catalog.setdefault(t.catalog, []).append(t)
        for catalog, group in by_catalog.items():
            clause = " OR ".join("(c.table_schema = ? AND c.table_name = ?)" for _ in group)
            params: list[str] = []
            for t in group:
                params += [t.schema or "", t.table]
            rows = self._ex.run(
                "SELECT c.table_schema AS schema_name, c.table_name AS table_name, "
                "t.table_type AS table_type, c.column_name AS column_name, "
                "c.ordinal_position AS ordinal, c.data_type AS data_type, "
                "c.is_nullable AS is_nullable, c.column_default AS column_default, "
                "c.comment AS comment "
                f"FROM {_q(catalog)}.information_schema.columns c "
                f"JOIN {_q(catalog)}.information_schema.tables t "
                "ON t.table_schema = c.table_schema AND t.table_name = c.table_name "
                f"WHERE {clause} ORDER BY c.table_schema, c.table_name, c.ordinal_position",
                params,
                timeout_s=self.timeout_s,
            )
            for r in rows:
                tref = ObjectRef(catalog, r["schema_name"], r["table_name"])
                nullable = None if r["table_type"] == "VIEW" else r["is_nullable"] == "YES"
                out.setdefault(tref, []).append(
                    Column(
                        ObjectRef(catalog, r["schema_name"], r["table_name"], r["column_name"]),
                        int(r["ordinal"]),
                        r["data_type"],
                        family_of(r["data_type"]),
                        nullable,
                        blank_to_none(r["comment"]),
                        r["column_default"],
                    )
                )
        return out

    def constraints(self, tables: Sequence[ObjectRef]) -> Mapping[ObjectRef, list[Constraint]]:
        return {t.table_ref(): [] for t in tables}

    def view_definitions(self, tables: Sequence[ObjectRef]) -> Mapping[ObjectRef, str]:
        """``<catalog>.information_schema.views.view_definition`` (the view's SQL; a catalog
        that does not expose it, or a materialized view it does not list, is absent)."""
        out: dict[ObjectRef, str] = {}
        by_catalog: dict[str, list[ObjectRef]] = {}
        for t in tables:
            if t.catalog is not None:
                by_catalog.setdefault(t.catalog, []).append(t)
        for catalog, group in by_catalog.items():
            clause = " OR ".join("(table_schema = ? AND table_name = ?)" for _ in group)
            params: list[str] = []
            for t in group:
                params += [t.schema or "", t.table]
            rows = self._ex.run(
                "SELECT table_schema AS schema_name, table_name AS table_name, "
                f"view_definition AS definition FROM {_q(catalog)}.information_schema.views "
                f"WHERE {clause} ORDER BY table_schema, table_name",
                params,
                timeout_s=self.timeout_s,
            )
            for r in rows:
                text = definition_text(r["definition"])
                if text is not None:
                    out[ObjectRef(catalog, r["schema_name"], r["table_name"])] = text
        return out

    def capabilities(self) -> ReaderCapabilities:
        return ReaderCapabilities(comments=True, view_definitions=True)


register_reader("trino", TrinoReader)
