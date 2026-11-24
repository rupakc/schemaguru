# Getting started

This page walks the shop warehouse from nothing to an agent-ready view. Every Python block
on it runs as written (the docs test executes them), in a directory holding the shop
warehouse as `warehouse.duckdb` and the example overlay as `schemaguru.yaml`; both are in
the repository's `examples/` folder (`python examples/duckdb_quickstart.py` builds the
warehouse).

## Install

```bash
pip install 'schemaguru[duckdb]'
```

SchemaGuru needs Python 3.10+. Its only required dependencies are FactLang and PyYAML; every
warehouse driver, LLM provider and agent framework is an optional extra (see the
[install matrix](index.md#install)). Until FactLang 0.1.0 is on PyPI, install it from git
first: `pip install "factlang @ git+https://github.com/rupakc/factlang@main"`.

## Connect

```python
import schemaguru as sg

guru = sg.Guru.connect("duckdb:///warehouse.duckdb")
print(guru)
```

`Guru.connect` opens the URL through FactLang's `connect()` when it is installed, and
otherwise opens `duckdb://` URLs with the DuckDB driver itself (read-only: SchemaGuru never
writes to a warehouse, and a mistyped path does not create a file). Other warehouses need
FactLang's `connect()` (FactLang 0.1.0); without it the URL fails with `SG106`.

* **The store.** Knowledge lives in one SQLite file, by default
  `./.schemaguru/<name>.db` (`name="default"`). Pass `store=` to put it elsewhere.
* **First use crawls.** When the store holds no table yet, `connect` crawls the catalog —
  metadata only (table and column names, types, keys, comments, catalog statistics),
  counted against the crawl budget. `crawl=False` skips it; `crawl=True` always crawls.
* **Paths.** `duckdb:///warehouse.duckdb` is relative to the working directory; an absolute
  POSIX path takes four slashes, `duckdb:////data/warehouse.duckdb`.
* **DuckDB file names.** DuckDB names a database's catalog after its file, so a file
  `shop.duckdb` holding a schema `shop` makes `shop.orders` ambiguous to DuckDB itself. Name
  the file differently from its schemas.

## Ask a question

```python
view = guru.context("monthly revenue by customer segment")
print(view.text)
print(view.tokens, "tokens;", [t.canonical() for t in view.tables])
```

Before anything is measured, the view holds catalog knowledge and says so:

```text
--8<-- "generated/quickstart_before.txt"
```

`context()` never spends warehouse money by itself: tables it shows that were never
measured are put on a queue (see [Measure](guide/measure.md)).

## Measure

```python
report = guru.measure("shop.orders")          # one table (names and globs accepted)
report = guru.measure(all=True)               # every table, hottest first
print(len(report.measured_tables), "tables measured;", report.bytes, "bytes")
for d in report.errors:
    print(d.code, d.message)
```

Measurement runs read-only FactLang probes within a budget (default: 1 GiB per probe, 10 GiB
per run). Small tables are measured exactly; tables above 50 GiB (or 500M rows) are sampled
and their numbers are labelled as estimates ([Sampling](guide/sampling.md)).

!!! warning "Until FactLang 0.1.0"
    With a FactLang that lacks `Checker.measure`, `measure()` writes nothing and returns
    `SG304` in `report.errors`. Everything else keeps working on catalog knowledge.

## Add what people know

A YAML overlay states what the data cannot: meaning, expected values, soft-delete rules,
pitfalls, glossary terms. Expectations are checked against measurement and never rewritten.

```python
for d in guru.load_overlays("schemaguru.yaml"):
    print(d.code, d.severity.value, d.message, d.location)
print(guru.context("refund rate by month").text)
```

The `[shop.orders]` block of that view, after measurement and the overlay:

```text
--8<-- "generated/orders_block.txt"
```

## Join paths

```python
ok = guru.find_path("shop.payments", "shop.customers")
print([t.canonical() for t in ok.paths[0].tables], ok.paths[0].coverage)

refused = guru.find_path("shop.orders", "shop.events")
print(refused.refusal, "-", refused.explanation)
```

## Hand it to an agent

```python
tools = guru.tools()                       # JSON-schema tool specs
print([t["name"] for t in tools])
result = guru.handle_tool_call("find_join_path", {"from": "orders", "to": "events"})
print(result["text"])
print(guru.prompt_block()[:400])           # a system-prompt fragment
guru.close()
```

See [Agents, tools and MCP](guide/agents.md) for adapters (LangChain, LlamaIndex, OpenAI
Agents, Anthropic) and `schemaguru mcp`.

## The same from the command line

```bash
schemaguru --url duckdb:///warehouse.duckdb crawl
schemaguru --url duckdb:///warehouse.duckdb measure --all
schemaguru context "monthly revenue by customer segment"
schemaguru path shop.orders shop.events      # exit code 1: the route is refused
schemaguru lint schemaguru.yaml              # check an overlay
```

Commands that only read knowledge (`context`, `search`, `path`, `report`, `mcp`) work on the
store alone. The full list is in the [CLI reference](reference/cli.md).

## Bring your own connection

`Guru.connect` is a convenience. Any metadata executor and measurement backend can be passed
in; here a DuckDB connection you already hold:

```python
import duckdb

from schemaguru.catalog import DBAPIExecutor, reader_for

conn = duckdb.connect("warehouse.duckdb", read_only=True)
mine = sg.Guru(
    reader=reader_for("duckdb", DBAPIExecutor(conn)),
    store=".schemaguru/mine.db",
    dialect="duckdb",
)
print(mine.crawl().tables_seen, "tables crawled")
mine.close()
conn.close()
```

`sg.Guru(connector=<a FactLang connector>)` builds the reader and the FactLang measurement
backend from the connector's dialect.
