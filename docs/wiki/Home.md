# SchemaGuru Wiki

> SchemaGuru is an open-source (MIT) Python library that discovers, measures and explains a data warehouse so an AI agent can query it without guessing.
> It reads the catalog, measures facts such as how well two columns really join, drafts meaning with an optional LLM, lets people correct it in YAML, and serves a small question-scoped view to the agent.
> This wiki explains why it exists, how it is built, what every component does and how to use it, in plain language.

## What problem does it solve?

An agent that writes SQL against a warehouse has to know things the table names do not tell it:
which column really joins to which, what values a status column actually holds, which rows are
soft-deleted, which join path multiplies rows. When it guesses wrong it does not get an error. It
gets a plausible, wrong number. SchemaGuru gives the agent that knowledge, and it says where every
piece came from (a catalog, a measurement, a person, or an LLM draft) so nothing unchecked passes
as checked. The longer story is on [[Why-Schema-Intelligence]].

## Three lines to a first context

```python
import schemaguru as sg
guru = sg.Guru.connect("duckdb:///shop.duckdb")      # crawls the catalog on first use (metadata only)
print(guru.context("monthly revenue by customer segment"))
```

`Guru.connect` opens the warehouse (SchemaGuru only ever reads from it), crawls the catalog into a local SQLite file
(`./.schemaguru/default.db`) when that file is empty, and `context()` returns the slice of the
warehouse relevant to the question, fitted to a token budget (4,000 by default). Asking a question
never spends warehouse money: tables it touches for the first time are only *queued* for
measurement (see [[Measurement]]).

## Where to start

| If you want to... | Read |
|---|---|
| understand the idea | [[Why-Schema-Intelligence]], [[Concepts]] |
| see how the pieces fit | [[Architecture]] |
| connect your warehouse | the `Warehouse-*` guides in the sidebar, then [[Catalog-and-Crawl]] |
| plug it into an agent | [[Agent-Integration]] |
| correct or add knowledge by hand | [[Overlays]] |
| see what a table is built from | [[Lineage]] |
| run it from a shell or CI | [[CLI]], [[Refresh-and-Drift]] |
| tune it | [[Configuration]], [[Scale-and-Cost]] |
| add a warehouse, store, detector... | the `Extending-*` pages |
| know what is safe | [[Security]], [[PII-and-Sampling-Policy]] |

## Status of this release

SchemaGuru is at version 0.2.1 (alpha). Everything described in this wiki exists on `main`
unless the page says otherwise. Two things matter for what you can do today:

- **Measurement needs FactLang.** SchemaGuru never writes measurement SQL itself; every number
  comes from [FactLang](https://github.com/rupakc/factlang) claims and primitives. The measurement
  capabilities (`Checker.measure`, sampling, join snippets, filter parsing) come with
  FactLang 0.1.4 or later, installed with SchemaGuru; with an older FactLang, `measure()`
  reports diagnostic `SG304` instead of measuring. `schemaguru doctor` shows which FactLang
  capabilities are present. The worked examples in this wiki were generated with the test
  suite's stand-in backend (reproducible byte for byte), and each one says so.
- **Server mode and the Postgres store are optional extras.** `schemaguru serve` (extra `server`) and
  `PostgresStore` (extra `postgres-store`) are for shared deployments; see [[Server]] and [[Store]].

## About the examples

Every example in this wiki uses one small, generic online shop: `customers`, `orders`,
`order_items`, `products`, `payments`, `events`, `accounts`, `users`, `sessions`. The same shop is
the test fixture in the repository
([`tests/fixtures/shop`](https://github.com/rupakc/schemaguru/tree/main/tests/fixtures/shop)), and
it is full of deliberate traps: an enum with case drift, dates stored as text, a soft-delete flag
that is TRUE or NULL but never FALSE, a thin-but-correct join, a broken join, and a hub table
that multiplies rows.

## See also

- [[Concepts]] - the vocabulary used everywhere else
- [[Architecture]] - the layers and the data flow, with diagrams
- [[FAQ]] - short answers to common questions
- [[Glossary]] - every term in one place
