# Measurement

> Measurement turns guesses into numbers: how many rows a table has, how often a column is NULL, which values an enum really takes, how well a join matches in each direction.
> Every probe is a read-only FactLang claim or primitive, priced before it runs and admitted against a byte budget; SchemaGuru never writes measurement SQL itself.
> It is lazy by default: nothing is measured until you ask, and an agent's question never spends warehouse money on its own.

## When measurement happens

| Trigger | What runs |
|---|---|
| `guru.measure("shop.orders")` (names or globs) | those tables |
| `guru.measure()` / `guru.measure_queue()` | drain the queue (tables first touched by a question, tables raised by feedback) |
| `guru.measure(all=True)` | every live table, hottest first |
| `guru.refresh()` | tables never measured, changed since their last measurement, or older than `fact_ttl_days` (14) |
| `schemaguru worker` | a foreground loop draining the queue every `--interval` seconds |
| `Config(measure_on_demand="background")` | a daemon thread drains the queue under the budget |
| `Config(measure_on_demand="block")` | `context()` measures its own unmeasured tables, capped at `block_timeout_s` (10 s) |

The default `measure_on_demand="queue"` means a question that scopes to an unmeasured table
renders it marked "unmeasured" and pushes it on the queue, and that is all. Spending is always
your explicit choice. Eager order (`all=True`) is: feedback count, plus the number of declared
foreign keys, plus `log(1 + row_count)`.

## What is measured

For each table the scheduler plans these probes, then runs everything admitted in one batch so
FactLang can fuse them:

| Probe (FactLang) | When | Result |
|---|---|---|
| column profile: row count, non-null and distinct per column (`ColumnAggregate`) | always | `ROW_COUNT`, `NULL_RATE`, `DISTINCT` |
| exact row count (`ROWS t`) | large tables only (a sampled profile cannot give it) | `ROW_COUNT` |
| primary-key uniqueness (`UNIQUE t.c [WITH [...]]`) | a declared PK, composite keys in full | `PRIMARY_KEY` |
| freshness (newest value) | timestamp and date columns | `FRESHNESS` |
| text-holds-dates (`PATTERN t.c LIKE '____-__-__%'`) | string columns whose name ends in `_at`, `_on`, `_ts`, `date` or `time` | `TYPE_NOTE` when ≥ 99% of values match |
| join overlap, both directions (`KeyOverlap`) | every join candidate of the table | `JOIN` |
| enum discovery: exact top values (`ValueMembership(declared=())`) | second phase, only columns with ≤ `enum_max_distinct` (20) distinct values | `VALUES` (observed), `PII` when values have a personal shape |

A declared list of values (from an overlay or dbt) is not a separate probe: when knowledge is
resolved, the observed values are compared with it (honouring `case_sensitive` and `values_split`),
and every observed value outside the list is shown as a contradiction with its row count. A
declared primary key is checked by the uniqueness probe, a declared join by the overlap probe.

Note the date probe: the column *name* only decides whether the pattern is worth checking; the
note is written only if the values actually match. A name is never taken as evidence.

## The join numbers

A `JOIN` fact carries both directions, because they are different facts:

| Field | Meaning |
|---|---|
| `match_rate` | distinct non-null child keys found in the parent / distinct non-null child keys |
| `coverage` | distinct parent keys reached by some child / distinct parent keys (the reverse direction) |
| `row_match_rate` | the same as `match_rate`, counted in rows |
| `fan_in` | child rows per distinct child key |
| `max_left_per_key`, `max_right_per_key` | the largest multiplicity on each side |
| `cardinality` | `N:1`, `1:1`, `1:N` or `N:M`, from those maxima |
| `verdict` | `OK` when match ≥ 0.95, `WEAK` when ≥ 0.05, else `BROKEN` (`GraphConfig.ok_threshold`, `weak_threshold`) |

On the shop fixture, `payments.order_id -> orders.order_id` has match 100% and coverage 5%:
every payment has an order, but only one order in twenty has a payment. Both numbers are shown,
and neither is a reason to refuse the join. See [[Join-Graph]].

## Where join candidates come from

A join is measured only if it is a *candidate*, and a candidate becomes a `JOIN` fact only
after measurement. In priority order:

1. declared foreign keys from the catalog;
2. joins in your overlays ([[Overlays]]);
3. dbt `relationships` tests (`schemaguru import-dbt`);
4. name and type heuristics: a column `x_id`, `xId` or `x_fk` pointing at a table named `x` or
   `xs` with a single-column primary key (else an `id` column) of a compatible type family; or a
   column whose name equals another table's single-column primary key in the same schema;
   at most 5 candidates per column, same catalog only;
5. joins an LLM suggested while drafting ([[Drafting]]).

Compatible families are equal families, or two numeric ones. A string key against a numeric key
is still a candidate, measured with `cast_to="string"` and flagged, because the agent will have
to cast too. A heuristic or LLM guess that was never measured leaves no fact at all.

## Budgets

`sg.Budget` (defaults in brackets):

| Field | Meaning |
|---|---|
| `max_bytes_per_probe` [1 GiB] | a probe estimated above this is not run: fact `ERROR`, reason `over_budget`, diagnostic `SG301`; other probes continue |
| `max_bytes_total` [10 GiB] | when spent, the run stops, `SG302`, and the remaining tables go back on the queue |
| `max_probes` [None] | probe count cap |
| `max_seconds` [None] | wall-clock cap |
| `crawl_bytes` [None] | cap for billed metadata bytes during a crawl |
| `draft_budget_tokens` [None] | token cap for a drafting run |
| `period_s` [86400] | a budget shared across runs (the background drainer, agent checks) refills once per period |

Every probe is priced first with FactLang's `Checker.estimate` (a dry run where the connector
supports it). Where FactLang cannot price it, SchemaGuru may use the catalog's table bytes,
labelled `heuristic`. A probe whose cost is still unknown is **charged pessimistically**: at
`max_bytes_per_probe`, else at `max_bytes_total`. If the warehouse then reports no bytes either,
that charge stays as the recorded spend (ledgered as `measure.unknown_bytes`), never as 0.
`MeasureReport.unknown_cost_probes` and `unknown_spend_probes` count them, so you know the byte
total is a lower bound.

Override for one run:

```python
guru.measure(all=True, budget=sg.Budget(max_bytes_total=2 << 30))
```

## Results and failures

Real output of `guru.measure(all=True)` on the shop fixture (2026-11-24; numbers from the test
suite's stand-in backend, which computes them with plain SQL on the same warehouse):

```text
MeasureReport(measured_tables=[...14 tables...], facts_written=121, over_budget=[], errors=[],
bytes=4512800, sampled_tables=[], checks_run={'TYPE_NOTE', 'PRIMARY_KEY', 'VALUES', 'JOIN',
'PII', 'DISTINCT', 'ROW_COUNT', 'NULL_RATE'}, unknown_cost_probes=0, unknown_spend_probes=0)
```

(Line-wrapped; the table list is abbreviated.)

Rules that hold for every result:

- A failed probe gives an `ERROR` fact for that subject only (`SG303`); the rest of the run is
  unaffected, and the error never replaces the last good measurement.
- An empty table gives `UNMEASURABLE`, which is different from a column that is entirely NULL
  (`NULL_RATE` 1.0, flagged as a dead column).
- Exact numbers are stored with `method="exact"`. A sampled number carries
  `sampled:<keyhash|system|bernoulli>:<fraction>:<seed>` and status `ESTIMATED`
  ([[Sampling]]). The two are never compared with each other.
- Rates are never rounded at storage; rendering rounds to 4 significant digits.
- Observed values pass through the table's value policy before they are stored
  ([[PII-and-Sampling-Policy]]).
- `checks_run` lists the fact kinds the run produced without error. A refresh uses it so that a
  check that did not run is reported as "not measured", never as "fixed".

## Without FactLang's measurement capability

If the installed FactLang lacks `Checker.measure` (true of FactLang before 0.1.0), `measure()`
returns a report with one `SG304` error and measures nothing; `schemaguru measure` exits 3:

```text
error SG304: the measurement backend cannot measure (the installed FactLang lacks Checker.measure, or no connector was given); nothing was measured (hint: pass backend=..., or upgrade FactLang)
measured 0 table(s), 0 fact(s), 0 bytes
```

(Real CLI output, 2026-11-24.) You can pass your own `backend=` implementing
`schemaguru.measure.MeasureBackend` in the meantime; that is what the test suite does.

## See also

- [[Sampling]] - how large tables are measured
- [[Scale-and-Cost]] - what a measurement run costs
- [[Join-Graph]] - how JOIN facts become graph edges
- [[Configuration]] - every budget and threshold
