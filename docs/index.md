# SchemaGuru

**Schema intelligence for AI agents on data warehouses.** SchemaGuru crawls a warehouse's
catalog, *measures* what really joins and which values really occur, refuses join paths that
multiply rows, lets people correct it in reviewable YAML, and hands an agent a
question-scoped, token-budgeted view of all of it — as prompt text, tool calls, an MCP
server or framework adapters.

```python
import schemaguru as sg
guru = sg.Guru.connect("duckdb:///warehouse.duckdb")
print(guru.context("monthly revenue by customer segment"))
```

!!! note "Status: alpha (0.1.0)"
    Measurement runs through [FactLang](https://github.com/rupakc/factlang) (0.1.4 or later,
    installed with SchemaGuru). Every output on this site was produced by the real library on
    the test **shop** warehouse; the measured numbers in the generated pages come from the test
    suite's backend, which computes them with plain SQL on that warehouse, so the pages are
    reproducible byte for byte. The cloud warehouse readers (BigQuery, Snowflake, Redshift,
    Databricks, Oracle) are tested against recorded metadata, not yet validated live.

## The problem: a wrong guess does not error

An agent that writes SQL against a warehouse knows what the names tell it. Names mislead in
at least three common ways, and each returns a plausible, wrong number instead of an error.
These three come from the shop warehouse SchemaGuru is tested on; the row counts are
counted with plain SQL on that warehouse.

### 1. The wrong join

--8<-- "generated/story_hub.md"

### 2. The soft-delete flag that is never FALSE

--8<-- "generated/story_soft_delete.md"

### 3. The enum that drifted in case

--8<-- "generated/story_case_drift.md"

## How SchemaGuru catches them

| Trap | What SchemaGuru does | Read more |
|---|---|---|
| Row-multiplying join | measures every candidate join in **both directions** (match rate, reverse coverage, fan-in); refuses a route that arrives at a hub on one many-to-one edge and leaves on another (`HUB_PIVOT`) | [Join graph](guide/graph.md) |
| TRUE/NULL flag | measures observed values *and* the null rate; shows the human's live-rows predicate next to them | [Measure](guide/measure.md) |
| Case drift | keeps a declared enum as an *expectation*, never rewrites it; reports what measurement saw beyond it | [Overlays](guide/overlays.md) |
| Narrow join | a thin but correct join is returned **labelled** with its coverage; only broken joins are refused | [Join graph](guide/graph.md) |
| Stale knowledge | refresh re-measures, diffs against the previous snapshot and classifies each change | [Refresh](guide/refresh.md) |

Every statement carries its provenance — *catalog*, *declared*, *measured*, *drafted* (by an
LLM) or *human* — and every measured number carries its method and date. Unknown is shown as
unknown, never as 0 or 100%. A view that shows a warning also says that the absence of a
warning is not verification.

## Before and after

What an agent often gets is the schema pasted into the prompt: every table, names and
types, nothing about what joins.

```text
--8<-- "generated/schema_dump.txt"
```

What it gets from `guru.context("monthly revenue by customer segment")` once the shop is
measured and its overlay loaded: the tables the question needs, with what was measured.

```text
--8<-- "generated/quickstart_after.txt"
```

Try more questions, the join-graph explorer and this comparison in the [demo](demo.md).

## Where it shines

<div class="sg-grid" markdown>
<div markdown>
**Agents on huge warehouses.** Metadata-only, paged, resumable crawls; search runs in the
store's full-text index, so a view is scoped without loading every table. Lazy, budgeted
measurement: an agent's question never spends warehouse money by itself.
</div>
<div markdown>
**Onboarding analysts.** `schemaguru context "<question>"` prints what a newcomer needs:
which tables answer it, which joins are measured, which columns hold traps.
</div>
<div markdown>
**Data-contract CI.** Declare enums, joins and soft-delete rules in YAML; `schemaguru
refresh` exits `1` when a change needs review, and view fingerprints detect prompt drift.
</div>
<div markdown>
**Migration audits.** Measure the same joins before and after a move; the refresh report
says which verdicts changed, judged relative to the previous snapshot.
</div>
</div>

Measured on the developer's machine (hardware-dependent): lexical table search over a
synthetic 500,000-table store had a p95 of about 167 ms (`tests/benchmark/`, a worst-case
vocabulary where one word matches a third of all tables); graph expansion over 50,000 joins
took a median of about 0.48 s (`benchmarks/bench_expand.py`). The whole `context` call at
500,000 tables is much slower (p95 28.6 s, a documented known limit): see
[Scale](guide/scale.md).

## Install

```bash
pip install schemaguru            # core: FactLang + PyYAML
pip install 'schemaguru[duckdb]'  # plus a warehouse driver (one extra per warehouse)
```

| Extra | Adds | Extra | Adds |
|---|---|---|---|
| `bigquery` | google-cloud-bigquery | `anthropic` | Anthropic drafting provider |
| `snowflake` | snowflake-connector-python | `openai` | OpenAI-compatible drafting provider |
| `redshift` | redshift-connector | `mcp` | `schemaguru mcp` (MCP SDK ≥ 2.3) |
| `databricks` | databricks-sql-connector | `server` | HTTP server (Starlette, uvicorn) |
| `postgres` | psycopg 3 | `langchain` | LangChain tools and retriever |
| `duckdb` | duckdb | `llamaindex` | LlamaIndex tools |
| `clickhouse` | clickhouse-connect | `openai-agents` | OpenAI Agents SDK tools |
| `mssql` | pyodbc (SQL Server, Synapse) | `embeddings` | numpy (embedding retriever) |
| `oracle` | oracledb | `tiktoken` | tiktoken token counter |
| `trino` | trino | `postgres-store` | Postgres knowledge store |
| `mysql` | PyMySQL (MySQL, MariaDB) | `all` | everything above |

The readers for PostgreSQL, MySQL, ClickHouse, SQL Server, Trino and DuckDB are tested
against real engines in CI. The BigQuery, Snowflake, Redshift, Databricks and Oracle readers
are built from the documented system views and tested on recorded metadata, but **not yet
validated against live accounts**.

Next: [Getting started](getting-started.md) · [Concepts](guide/concepts.md) ·
[Demo](demo.md)
