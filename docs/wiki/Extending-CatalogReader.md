# Extending: CatalogReader

> A `CatalogReader` teaches SchemaGuru a new warehouse: it turns the warehouse's system views into `Schema`, `Table`, `Column` and `Constraint` objects.
> Implement six methods, register a factory under a dialect name with `register_reader`, and `sg.catalog.reader_for(dialect, executor)` will find it.
> Readers are metadata-only, bind every value, and report unknown as `None`.

## The protocol

`schemaguru.catalog.CatalogReader` (spec 6.1):

```python
class CatalogReader(Protocol):
    dialect: str
    def list_catalogs(self) -> Iterator[str]: ...                       # may yield nothing
    def list_schemas(self, catalog: str | None, *, page_token: str | None) -> Page[Schema]: ...
    def list_tables(self, schema: Schema, *, page_token: str | None,
                    changed_since: datetime | None) -> Page[Table]: ...
    def columns(self, tables: Sequence[ObjectRef]) -> Mapping[ObjectRef, list[Column]]: ...
    def constraints(self, tables: Sequence[ObjectRef]) -> Mapping[ObjectRef, list[Constraint]]: ...
    def capabilities(self) -> ReaderCapabilities: ...
```

- `Page(items, next_token)`: return `next_token=None` on the last page. List schemas in a stable
  order (the crawl's resume relies on it).
- `list_tables(..., changed_since=None)` returns every live table; with a timestamp, only tables
  altered since (meaningful only when `capabilities().incremental`).
- `columns` and `constraints` are batched: the crawl passes up to `crawl.column_batch` (200)
  tables per call.
- `ReaderCapabilities(comments, stats, last_altered, constraints, incremental, cost_estimate)`,
  all `False` by default: claim only what you really provide.
- Optional attributes the crawl uses if present: `page_size`, `timeout_s` (set from the config),
  `bytes_billed` and `bytes_billed_is_lower_bound` (for billed metadata).

Rules for SQL-based readers: only system views; write SQL with `?` placeholders and bind every
value; run it through the `MetadataExecutor` you are given (`executor.run(sql, params,
timeout_s=...)`), which converts placeholders to the driver's style and refuses anything but one
read-only statement (`SG104`). Identifiers that cannot be bound must be validated against a
strict character set. Names are returned exactly as the catalog stores them.

## Worked example

A reader over a plain Python dict (a stand-in for "some API that lists tables"). It was run
against SchemaGuru 0.1.0 for this page:

```python
from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime

import schemaguru as sg
from schemaguru.catalog import Page, ReaderCapabilities, register_reader
from schemaguru.model.structure import Column, Constraint, Schema, Table


class DictReader:
    """A reader over a plain dict: {schema: {table: [(column, native_type, family), ...]}}."""

    dialect = "dictdb"

    def __init__(self, layout: Mapping[str, Mapping[str, list[tuple[str, str, str]]]]) -> None:
        self.layout = layout

    def list_catalogs(self) -> Iterator[str]:
        return iter(())                       # one default catalog: refs carry no catalog

    def list_schemas(self, catalog: str | None, *, page_token: str | None) -> Page[Schema]:
        return Page([Schema(None, name) for name in sorted(self.layout)])

    def list_tables(
        self, schema: Schema, *, page_token: str | None, changed_since: datetime | None
    ) -> Page[Table]:
        names = sorted(self.layout.get(schema.name, {}))
        return Page([Table(sg.ObjectRef(None, schema.name, t), "table") for t in names])

    def columns(self, tables: Sequence[sg.ObjectRef]) -> Mapping[sg.ObjectRef, list[Column]]:
        out: dict[sg.ObjectRef, list[Column]] = {}
        for ref in tables:
            cols = self.layout[ref.schema][ref.table]
            out[ref] = [
                Column(sg.ObjectRef(None, ref.schema, ref.table, name), i, native, family)
                for i, (name, native, family) in enumerate(cols, start=1)
            ]
        return out

    def constraints(self, tables: Sequence[sg.ObjectRef]) -> Mapping[sg.ObjectRef, list[Constraint]]:
        return {ref: [] for ref in tables}    # unknown: no constraints declared

    def capabilities(self) -> ReaderCapabilities:
        return ReaderCapabilities()           # no comments, stats, constraints or incremental


# a factory takes the metadata executor; this reader does not need it
LAYOUT = {"shop": {"orders": [("order_id", "INT", "integer"), ("status", "TEXT", "string")]}}
register_reader("dictdb", lambda executor: DictReader(LAYOUT))
```

```python
from schemaguru.catalog import reader_for
guru = sg.Guru(reader=reader_for("dictdb", None), dialect="dictdb", store="dict.db")
guru.crawl()
print(guru.context("orders by status").text)
```

Real output:

```text
[shop.orders]
  unmeasured: nothing here has been measured yet; figures are declared or drafted
  Columns:
    order_id  INT
    status    TEXT
  Provenance: catalog only

Not shown: no other candidate tables.
```

Type `family` values are FactLang's type families (`integer`, `decimal`, `float`, `string`,
`boolean`, `date`, `timestamp`, `other`, ...). The shipped readers map native types with
`schemaguru.catalog.readers._types.family_of`.

## Test template

The repository's reader conformance suite replays recorded system-view rows
([`tests/conformance`](https://github.com/rupakc/schemaguru/tree/main/tests/conformance)). For a
new reader, start with:

```python
from schemaguru.catalog import crawl
from schemaguru.store.sqlite import SQLiteStore

def test_my_reader_crawls(tmp_path):
    store = SQLiteStore(tmp_path / "s.db")
    report = crawl(DictReader(LAYOUT), store)
    assert report.errors == [] and report.tables_written == 1
    cols = store.columns(sg.ObjectRef(None, "shop", "orders"))
    assert [c.ref.column for c in cols] == ["order_id", "status"]
    assert store.table(sg.ObjectRef(None, "shop", "orders")).row_count is None   # unknown stays None

def test_negative_control_unknown_schema_is_empty():
    assert DictReader(LAYOUT).list_tables(Schema(None, "nope"), page_token=None,
                                          changed_since=None).items == []
```

An installed package can also declare it in the entry-point group `schemaguru.readers`
(name = dialect, object = the factory); `reader_for` and `dialects()` load the group on first
use. A plugin that fails to load, or reuses a built-in dialect name, is skipped with `SG108`.

## See also

- [[Catalog-and-Crawl]]
- The `Warehouse-*` pages, for what real readers deal with
- [[Contributing]]
- [[Architecture]]
