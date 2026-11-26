# SchemaGuru

[![CI](https://github.com/rupakc/schemaguru/actions/workflows/ci.yml/badge.svg)](https://github.com/rupakc/schemaguru/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/schemaguru.svg)](https://pypi.org/project/schemaguru/)
[![Python versions](https://img.shields.io/pypi/pyversions/schemaguru.svg)](https://pypi.org/project/schemaguru/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/rupakc/schemaguru/blob/main/LICENSE)
[![Docs](https://img.shields.io/badge/docs-rupakc.github.io%2Fschemaguru-indigo.svg)](https://rupakc.github.io/schemaguru/)

**Schema intelligence for AI agents on data warehouses.** SchemaGuru crawls a warehouse's
catalog, *measures* what really joins and which values really occur, refuses join paths that
multiply rows, lets people correct it in reviewable YAML, and hands an agent a
question-scoped, token-budgeted view of all of it: as prompt text, tool calls, an MCP server
or framework adapters.

## Why

An agent that writes SQL against a warehouse knows what the names tell it, and names mislead.
A join through a hub table multiplies rows; a soft-delete flag is `TRUE` or `NULL` but never
`FALSE`, so `WHERE NOT is_deleted` drops every live row; an enum drifted in case, so
`status = 'paid'` misses part of the paid orders. None of these raises an error: each returns
a plausible, wrong number. SchemaGuru measures these facts in the warehouse and puts them in
front of the agent, each labelled with where it came from.

## Install

```bash
pip install schemaguru            # core: FactLang + PyYAML
pip install 'schemaguru[duckdb]'  # plus a warehouse driver (one extra per warehouse)
```

Python 3.10 to 3.13 (the Postgres store needs PostgreSQL 11 or later). Every warehouse driver, LLM provider and agent framework is an optional
extra:

| Extra | Adds | Extra | Adds |
|---|---|---|---|
| `bigquery` | google-cloud-bigquery | `anthropic` | Anthropic drafting provider |
| `snowflake` | snowflake-connector-python | `openai` | OpenAI-compatible drafting provider |
| `redshift` | redshift-connector | `mcp` | MCP server (stdio; HTTP also needs the `server` extra) |
| `databricks` | databricks-sql-connector | `server` | HTTP server (Starlette, uvicorn) |
| `postgres` | psycopg 3 | `langchain` | LangChain tools and retriever |
| `duckdb` | duckdb | `llamaindex` | LlamaIndex tools |
| `clickhouse` | clickhouse-connect | `openai-agents` | OpenAI Agents SDK tools |
| `mssql` | pyodbc (SQL Server, Synapse) | `embeddings` | numpy (embedding retriever) |
| `oracle` | oracledb | `tiktoken` | tiktoken token counter |
| `trino` | trino | `postgres-store` | Postgres knowledge store (teams, server; PostgreSQL 11+) |
| `mysql` | PyMySQL (MySQL, MariaDB) | `all` | everything above |

## Quickstart

```python
import schemaguru as sg
guru = sg.Guru.connect("duckdb:///warehouse.duckdb")
print(guru.context("monthly revenue by customer segment"))
```

`connect` crawls the catalog on first use (metadata only) into a local store,
`./.schemaguru/default.db`. `context` returns the tables the question needs, with what is
known about them. Nothing has been measured yet, and the view says so. Measure, then ask
again:

```python
guru.measure(all=True)  # within the default budget; large tables are sampled and labelled
print(guru.context("monthly revenue by customer segment"))
path = guru.handle_tool_call("find_join_path", {"from": "orders", "to": "customers"})
print(path["text"])  # the same JSON tool an agent calls
guru.close()
```

The same from a terminal:

```bash
schemaguru crawl --url duckdb:///warehouse.duckdb
schemaguru measure --all --url duckdb:///warehouse.duckdb
schemaguru context "monthly revenue by customer segment"
```

Other warehouses connect by URL (`snowflake://…`, `postgresql://…`) or through the
connection a host already holds: `sg.Guru(connector=conn)` accepts a DB-API connection, a
SQLAlchemy engine, a BigQuery client or a plain `execute(sql)` function.

### Lineage

```python
guru.import_dbt("target/manifest.json")   # or just crawl: view definitions are parsed too
print(guru.handle_tool_call("lineage", {"ref": "analytics.revenue_daily", "depth": 2})["text"])
```

What a table or column is built from (or feeds, with `"direction": "downstream"`), declared or
parsed and never measured or verified, and never a join path. Parsing SQL needs
`pip install 'schemaguru[lineage]'`; see the [lineage guide](https://rupakc.github.io/schemaguru/guide/lineage/).

## Features

| Feature | What it does |
|---|---|
| **Catalog crawl** | metadata only; paged, resumable and incremental; include/exclude globs; billed metadata bytes reported and capped (`Budget.crawl_bytes`) |
| **Measurement** | join match in both directions, fan-out, uniqueness, null rates, observed enum values, freshness; every number carries its method and date; budgeted per probe and per period; large tables sampled by key hash and labelled `ESTIMATED`, never cut with `LIMIT` |
| **Join graph** | measured and declared edges; path search returns thin joins labelled and refuses broken ones and row-multiplying hub pivots |
| **Lineage** | table and column lineage from dbt manifests, view definitions (parsed in a limited worker process; only a hash and a literal-free template are stored) and overlays; the `lineage` tool for agents; declared or parsed, never a join path |
| **Human overlays** | reviewable YAML for descriptions, enums, soft-delete rules, join recipes and waivers; humans always win over drafts; dbt manifests import |
| **LLM drafting** | optional descriptions and pitfalls from Anthropic or OpenAI-compatible models, checked against measured facts and accepted through review |
| **Scoped views** | a question-scoped, token-budgeted view per agent role, as text, Markdown or JSON; byte-deterministic, with fingerprints for prompt-drift checks in CI |
| **Agent surface** | JSON tool specs and a dispatcher; an MCP server over stdio and streamable HTTP; LangChain, LlamaIndex, OpenAI Agents and Anthropic adapters |
| **Refresh and drift** | re-crawl, re-measure, diff against the previous snapshot and classify each change; exit codes for CI |
| **Server** | optional HTTP service over a shared Postgres store, with bearer tokens and one refresh at a time across replicas |

At 500,000 tables and 25,000,000 columns a full crawl takes about 9.4 minutes in 63 MiB, but
`context` is slow there: p95 28.6 s against a 200 ms target, which is **not met** (the cost is
the join-path search). Numbers, machine and reasons: the
[Scale page](https://rupakc.github.io/schemaguru/guide/scale/).

Every statement carries its provenance (*catalog*, *declared*, *measured*, *drafted* or
*human*). Unknown is shown as unknown, never as 0 or 100%.

## Privacy defaults

- Values reach an LLM **masked** by default: anything that looks personal (emails, phones,
  IBANs, cards, IPs, UUIDs, long hex strings, secrets, names, addresses, free text) is replaced
  by its shape, such as `<email>`. `raw` is an explicit opt-in, and `none` sends no values at
  all. The mode can be set per table glob (case-insensitive; on a tie the most restrictive
  wins).
- Only the top values of low-cardinality columns and a few example values per column are
  read, never whole rows. Raw samples are not stored unless you opt in.
- A final sweep masks anything personal or credential-shaped in every outbound payload, and
  error messages are scrubbed of connection credentials on every surface.
- SchemaGuru never writes to a warehouse. An agent's question spends warehouse money only
  when you allow it (`Config.measure_on_demand`, `Config.agent_checks`).

## Supported warehouses

| Warehouse | Status |
|---|---|
| DuckDB, PostgreSQL, MySQL / MariaDB, ClickHouse, SQL Server, Trino | catalog readers tested against real engines in CI (SQL Server 2022) |
| Azure Synapse | supported through the T-SQL reader; not CI-tested |
| BigQuery, Snowflake, Redshift, Databricks, Oracle | built from the documented system views and tested on recorded metadata; **not yet validated against live accounts** |

A warehouse that is not listed needs a
[custom catalog reader](https://rupakc.github.io/schemaguru/extending/catalog-reader/).

## Built on FactLang

SchemaGuru writes no measurement SQL. It states what it wants to know as
[FactLang](https://github.com/rupakc/factlang) measurements and claims; FactLang renders
dialect-correct SQL, runs it and reports the evidence. FactLang (`factlang>=0.1.4,<0.2`) is
installed with SchemaGuru.

## Documentation

- Documentation site: <https://rupakc.github.io/schemaguru/> (guides, API reference,
  interactive demo)
- Wiki: <https://github.com/rupakc/schemaguru/wiki> (concepts, each component, warehouse
  guides, extending, FAQ)
- Changelog: [CHANGELOG.md](https://github.com/rupakc/schemaguru/blob/main/CHANGELOG.md)

## Contributing

Contributions are welcome. [CONTRIBUTING.md](https://github.com/rupakc/schemaguru/blob/main/CONTRIBUTING.md)
has the setup, the checks CI runs and the project rules;
[SECURITY.md](https://github.com/rupakc/schemaguru/blob/main/SECURITY.md) says how to report a
vulnerability.

## License

MIT. Copyright (c) 2026 Rupak Chakraborty. See
[LICENSE](https://github.com/rupakc/schemaguru/blob/main/LICENSE).
