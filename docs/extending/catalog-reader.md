# Catalog reader

A reader teaches SchemaGuru a new warehouse's catalog. It is metadata-only: it reads system
views (or a client API), never data rows, and answers `None` for what it does not know.

## Protocol

```py
class CatalogReader(Protocol):
    dialect: str
    def list_catalogs(self) -> Iterator[str]: ...            # may yield nothing
    def list_schemas(self, catalog: str | None, *, page_token: str | None) -> Page[Schema]: ...
    def list_tables(self, schema: Schema, *, page_token: str | None,
                    changed_since: datetime | None) -> Page[Table]: ...
    def columns(self, tables: Sequence[ObjectRef]) -> Mapping[ObjectRef, list[Column]]: ...
    def constraints(self, tables: Sequence[ObjectRef]) -> Mapping[ObjectRef, list[Constraint]]: ...
    def capabilities(self) -> ReaderCapabilities: ...
```

* `list_tables` returns every live table when `changed_since` is `None`; otherwise only those
  altered since (meaningful only when `capabilities().incremental` is true).
* `columns` and `constraints` are batched: the crawl passes up to `column_batch` tables.
* Optional `view_definitions(tables) -> Mapping[ObjectRef, str]` (set `ReaderCapabilities(view_definitions=True)`):
  the SQL text of views and materialized views, batched like `columns`. Leave both out and the
  reader works unchanged. A definition you cannot read is absent, never guessed; the text is raw
  SQL that may hold PII, so never log it or put it in an error.
* `Page(items, next_token)`: `next_token=None` on the last page. List in a stable order, so an
  interrupted crawl can resume.
* Raise `CatalogError(code, message)` for a failure of the whole call; the crawl records it
  (`SG102`) and skips that schema, page or batch.
* `Column.family` is a FactLang type family (`integer`, `decimal`, `float`, `string`,
  `boolean`, `date`, `timestamp`, …).

## Worked example: a catalog from a dict

A reader for a system that publishes its catalog as JSON (here, an in-memory dict).

```python
from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime

import schemaguru as sg
from schemaguru.catalog import Page, ReaderCapabilities, register_reader
from schemaguru.model.structure import Column, Constraint, Schema, Table

CATALOG = {
    "shop": {
        "orders": [("order_id", "BIGINT", "integer"), ("status", "TEXT", "string")],
        "customers": [("customer_id", "BIGINT", "integer"), ("segment", "TEXT", "string")],
    }
}


class DictReader:
    dialect = "dictdb"

    def __init__(self, catalog: Mapping[str, Mapping[str, list]]) -> None:
        self.catalog = catalog

    def list_catalogs(self) -> Iterator[str]:
        return iter(())

    def list_schemas(self, catalog: str | None, *, page_token: str | None) -> Page[Schema]:
        return Page([Schema(None, name) for name in sorted(self.catalog)])

    def list_tables(self, schema: Schema, *, page_token: str | None,
                    changed_since: datetime | None) -> Page[Table]:
        names = sorted(self.catalog.get(schema.name, {}))
        return Page([Table(sg.ObjectRef(None, schema.name, n), "table") for n in names])

    def columns(self, tables: Sequence[sg.ObjectRef]) -> Mapping[sg.ObjectRef, list[Column]]:
        out = {}
        for t in tables:
            cols = self.catalog[t.schema][t.table]
            out[t] = [Column(sg.ObjectRef(None, t.schema, t.table, name), i, native, family)
                      for i, (name, native, family) in enumerate(cols, start=1)]
        return out

    def constraints(self, tables: Sequence[sg.ObjectRef]) -> Mapping[sg.ObjectRef, list[Constraint]]:
        return {t: [Constraint("primary_key", t, (self.catalog[t.schema][t.table][0][0],))]
                for t in tables}

    def capabilities(self) -> ReaderCapabilities:
        return ReaderCapabilities(constraints=True)


guru = sg.Guru(reader=DictReader(CATALOG), store=".schemaguru/dict.db", dialect="dictdb")
print(guru.crawl().tables_seen, [t.ref.canonical() for t in guru.store.iter_tables()])
print(guru.context("orders by status").text)
guru.close()
```

To have `Guru(connector=...)` and `reader_for(dialect, executor)` build it, register a
factory taking a metadata executor (`execute(sql, params)` → rows; see
`sg.catalog.MetadataExecutor`):

```python
register_reader("dictdb", lambda executor: DictReader(CATALOG))
print("dictdb" in sg.catalog.dialects())
```

A SQL-backed reader runs its metadata queries through the executor it is given — a
`FactLangExecutor` over a FactLang connector, or a `DBAPIExecutor` over a DB-API connection —
always with bind parameters for values. The shipped readers in `schemaguru/catalog/readers/`
are the reference.

## Test template

The shipped readers are tested against recorded system-view rows and, where a container
exists, a live engine (`tests/conformance/test_readers.py`, `record.py`, `replay.py`). For
your own reader, test the contract the crawl relies on:

```py
def test_reader_contract():
    reader = DictReader(CATALOG)
    schemas = reader.list_schemas(None, page_token=None)
    assert schemas.next_token is None and [s.name for s in schemas.items] == ["shop"]
    tables = reader.list_tables(schemas.items[0], page_token=None, changed_since=None).items
    cols = reader.columns([t.ref for t in tables])
    assert all(c.ref.table == t.ref.table for t in tables for c in cols[t.ref])
    # unknown stays None, never a default
    assert all(t.row_count is None for t in tables)
```
