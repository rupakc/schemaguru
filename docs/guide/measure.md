# Measure

Measurement turns guesses into numbers. Every number comes from a read-only FactLang probe
— SchemaGuru never writes measurement SQL itself — and is stored with its method and time.

!!! warning "Needs FactLang 0.1.0"
    The probes run through FactLang's `Checker.measure`. With a FactLang that lacks it,
    `measure()` returns `SG304` and writes nothing. The examples on this page show measured
    output because the docs test supplies the test suite's backend.

## What is measured

| Probe (FactLang) | Fact | Notes |
|---|---|---|
| row count (`ROWS`, or catalog statistics) | `ROW_COUNT` | never sampled |
| non-null and distinct counts, fused per table | `NULL_RATE`, `DISTINCT` | an empty table is `UNMEASURABLE`; an all-null column is a dead column (`NULL_RATE = 1.0`) |
| top observed values, when distinct ≤ `enum_max_distinct` (20) | `VALUES` (observed, with counts) | exact top-k, never random rows |
| declared values (`VALUES … IN [...]`) | `VALUES` status | the human list is checked, never edited |
| key uniqueness (`UNIQUE`, composite keys in full) | `PRIMARY_KEY` status | |
| joins, both directions (`KeyOverlap`) | `JOIN` | match rate, coverage, row match, fan-in, cardinality, verdict |
| string patterns (`PATTERN … LIKE`) | `TYPE_NOTE` | e.g. ISO dates or epoch numbers stored as text |
| freshness (`FRESH`) on timestamp columns | `FRESHNESS` | never sampled |

```python
import schemaguru as sg

guru = sg.Guru.connect("duckdb:///warehouse.duckdb")
report = guru.measure(["shop.orders", "shop.customers"])
print([t.canonical() for t in report.measured_tables], report.facts_written, "facts")
print("checks:", sorted(report.checks_run))

join = next(
    f for f in guru.store.facts_for(None, ["JOIN"])
    if f.subject.left[0].table == "orders" and f.subject.left[0].column == "customer_id"
)
v = join.value
print(v["match_rate"], v["coverage"], v["cardinality"], v["verdict"], join.method)
```

Rates are stored unrounded; views round them (four significant digits).

## Which joins are measured

A join is measured only if it is a **candidate**, in this order: declared foreign keys from
the catalog; joins from overlays and dbt `relationships` tests; name and type heuristics
(`x_id`, `xId`, `x_fk` → a table `x` or `xs` with a compatible key; a column named like
another table's single-column primary key in the same schema; at most 5 per column, same
catalog only); and joins an LLM draft suggested. A candidate is never shown as a join until
it is measured. Keys of different type families (text vs integer) are measured with a cast
and flagged, because the agent must cast too.

## Lazy by default

An agent's question never spends warehouse money by itself (`Config.measure_on_demand`):

| Mode | What `context()` does with an unmeasured table it shows |
|---|---|
| `queue` (default) | marks it *unmeasured* and puts it on the queue; nothing runs |
| `background` | the same, and one daemon thread drains the queue under the budget |
| `block` | measures the scoped tables before answering, at most `block_timeout_s` (10 s) |

The queue is drained by `guru.measure()` (no targets) or `guru.measure_queue()`,
`guru.refresh()`, or `schemaguru worker`.

```python
lazy = sg.Guru.connect("duckdb:///warehouse.duckdb", store=".schemaguru/lazy.db")
lazy.context("events per account and session")          # queues what it showed
print(lazy.measure_queue().measured_tables)              # an explicit spend
lazy.close()
```

## Budgets

Every probe is priced first (a dry run where the warehouse supports one; otherwise catalog
bytes, labelled `heuristic`). `sg.Budget`:

| Field | Default | Effect |
|---|---|---|
| `max_bytes_per_probe` | 1 GiB | a probe over it is not run: fact `ERROR over_budget` (`SG301`) |
| `max_bytes_total` | 10 GiB | when spent, the run stops and the rest stays queued (`SG302`) |
| `max_probes`, `max_seconds` | none | further caps per run |
| `period_s` | 1 day | a budget shared across runs (the background drainer, agent checks) refills once per period |

A probe whose cost is unknown is charged **pessimistically** (at the per-probe limit) and,
if the warehouse reports no bytes either, recorded as unknown spend, never as 0. Every byte
goes to the store's budget ledger.

```python
small = guru.measure("shop.events", budget=sg.Budget(max_bytes_total=64 << 20, max_probes=20))
print(small.bytes, "bytes;", small.unknown_cost_probes, "probes of unknown cost;",
      [t.canonical() for t in small.over_budget], "over budget")
guru.close()
```

## Failure is local

A probe that fails produces an `ERROR` fact for that subject only (`SG303`); the run goes
on. Asking to measure a table that was never crawled is `SG305`.

See also: [Sampling large tables](sampling.md) · [Cost and scale](cost-and-scale.md).
