"""A synthetic ``CatalogReader`` that generates N tables lazily (never holds them all)."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from schemaguru.catalog.protocol import Page, ReaderCapabilities
from schemaguru.model.refs import ObjectRef
from schemaguru.model.structure import Column, Constraint, Schema, Table

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


class FakeReader:
    dialect = "fake"

    def __init__(
        self,
        n_tables: int,
        *,
        tables_per_schema: int = 1000,
        page_size: int = 1000,
        incremental: bool = False,
        fail_schemas: Sequence[str] = (),
        interrupt_at_call: int | None = None,
        altered: Callable[[int], datetime] | None = None,
        constraints: bool = True,
    ) -> None:
        self.n_tables = n_tables
        self.tps = tables_per_schema
        self.page_size = page_size
        self.incremental = incremental
        self.fail_schemas = set(fail_schemas)
        self.interrupt_at_call = interrupt_at_call
        self.altered = altered or (lambda n: BASE + timedelta(seconds=n))
        self.with_constraints = constraints
        self.dropped: set[int] = set()
        self.table_calls: list[dict[str, Any]] = []
        self.column_batches: list[int] = []

    # -- helpers -----------------------------------------------------------------------------
    def n_schemas(self) -> int:
        return (self.n_tables + self.tps - 1) // self.tps

    @staticmethod
    def schema_name(i: int) -> str:
        return f"s{i:04d}"

    @staticmethod
    def table_name(n: int) -> str:
        return f"t{n:07d}"

    def _indices(self, schema: str, changed_since: datetime | None) -> Iterator[int]:
        i = int(schema[1:])
        for n in range(i * self.tps, min((i + 1) * self.tps, self.n_tables)):
            if n in self.dropped:
                continue
            if changed_since is not None and self.altered(n) <= changed_since:
                continue
            yield n

    # -- protocol ----------------------------------------------------------------------------
    def list_catalogs(self) -> Iterator[str]:
        return iter(())

    def list_schemas(self, catalog: str | None, *, page_token: str | None) -> Page[Schema]:
        return Page([Schema(None, self.schema_name(i)) for i in range(self.n_schemas())], None)

    def list_tables(
        self, schema: Schema, *, page_token: str | None, changed_since: datetime | None
    ) -> Page[Table]:
        self.table_calls.append(
            {"schema": schema.name, "token": page_token, "changed_since": changed_since}
        )
        if self.interrupt_at_call is not None and len(self.table_calls) == self.interrupt_at_call:
            self.interrupt_at_call = None
            raise KeyboardInterrupt
        if schema.name in self.fail_schemas:
            raise RuntimeError(f"boom in {schema.name}")
        start = int(page_token) if page_token else 0
        wanted: list[int] = []
        more = False
        for k, n in enumerate(self._indices(schema.name, changed_since)):
            if k < start:
                continue
            if len(wanted) == self.page_size:
                more = True
                break
            wanted.append(n)
        items = [
            Table(
                ObjectRef(None, schema.name, self.table_name(n)),
                "table",
                row_count=n,
                last_altered=self.altered(n),
            )
            for n in wanted
        ]
        return Page(items, str(start + len(wanted)) if more else None)

    def columns(self, tables: Sequence[ObjectRef]) -> Mapping[ObjectRef, list[Column]]:
        self.column_batches.append(len(tables))
        return {
            t: [
                Column(
                    ObjectRef(t.catalog, t.schema, t.table, "id"), 1, "BIGINT", "integer", False
                ),
                Column(ObjectRef(t.catalog, t.schema, t.table, "name"), 2, "TEXT", "string", True),
            ]
            for t in tables
        }

    def constraints(self, tables: Sequence[ObjectRef]) -> Mapping[ObjectRef, list[Constraint]]:
        return {t: [Constraint("primary_key", t, ("id",), enforced=True)] for t in tables}

    def capabilities(self) -> ReaderCapabilities:
        return ReaderCapabilities(
            comments=False,
            stats=True,
            last_altered=self.incremental,
            constraints=self.with_constraints,
            incremental=self.incremental,
        )


class ViewFakeReader:
    """Named tables and views (dialect ``duckdb``) with view definitions.

    ``relations`` maps ``"schema.name"`` to column names; ``views`` maps some of those keys to
    their definition. Mutate ``relations`` / ``views`` / ``altered`` between crawls.
    """

    dialect = "duckdb"

    def __init__(
        self,
        relations: Mapping[str, Sequence[str]],
        views: Mapping[str, str] | None = None,
        *,
        incremental: bool = False,
        definitions: bool = True,
        fail_definitions: bool = False,
        interrupt_schema: str | None = None,
    ) -> None:
        self.relations = dict(relations)
        self.views = dict(views or {})
        self.incremental = incremental
        self.definitions = definitions
        self.fail_definitions = fail_definitions
        self.interrupt_schema = interrupt_schema
        self.altered: dict[str, datetime] = {k: BASE for k in self.relations}
        self.definition_calls: list[list[str]] = []

    @staticmethod
    def _ref(key: str) -> ObjectRef:
        schema, _, name = key.partition(".")
        return ObjectRef(None, schema, name)

    def list_catalogs(self) -> Iterator[str]:
        return iter(())

    def list_schemas(self, catalog: str | None, *, page_token: str | None) -> Page[Schema]:
        names = sorted({k.partition(".")[0] for k in self.relations})
        return Page([Schema(None, n) for n in names], None)

    def list_tables(
        self, schema: Schema, *, page_token: str | None, changed_since: datetime | None
    ) -> Page[Table]:
        if self.interrupt_schema == schema.name:
            self.interrupt_schema = None
            raise KeyboardInterrupt
        items = [
            Table(
                self._ref(k),
                "view" if k in self.views else "table",
                last_altered=self.altered.get(k, BASE),
            )
            for k in sorted(self.relations)
            if k.partition(".")[0] == schema.name
            and (changed_since is None or self.altered.get(k, BASE) > changed_since)
        ]
        return Page(items, None)

    def columns(self, tables: Sequence[ObjectRef]) -> Mapping[ObjectRef, list[Column]]:
        out: dict[ObjectRef, list[Column]] = {}
        for t in tables:
            names = self.relations.get(f"{t.schema}.{t.table}", ())
            out[t] = [
                Column(ObjectRef(t.catalog, t.schema, t.table, c), n, "INT", "integer", True)
                for n, c in enumerate(names, start=1)
            ]
        return out

    def constraints(self, tables: Sequence[ObjectRef]) -> Mapping[ObjectRef, list[Constraint]]:
        return {}

    def view_definitions(self, tables: Sequence[ObjectRef]) -> Mapping[ObjectRef, str]:
        keys = [f"{t.schema}.{t.table}" for t in tables]
        self.definition_calls.append(keys)
        if self.fail_definitions:
            raise RuntimeError("driver said: SELECT secret_column FROM vault WHERE pin = 1234")
        return {t: self.views[k] for t, k in zip(tables, keys, strict=True) if k in self.views}

    def capabilities(self) -> ReaderCapabilities:
        return ReaderCapabilities(
            comments=False,
            stats=False,
            last_altered=self.incremental,
            constraints=False,
            incremental=self.incremental,
            view_definitions=self.definitions,
        )
