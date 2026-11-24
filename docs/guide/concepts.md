# Concepts

SchemaGuru keeps everything it knows about a warehouse as **facts** in a local **store**,
and renders the slice a question needs as a **view**. Five ideas explain almost every
behaviour.

## Facts

A `Fact` is one statement about one subject — a table, a column, a join, or the warehouse
as a whole (glossary terms, metrics):

| Field | Meaning |
|---|---|
| `kind` | what is stated: `ROW_COUNT`, `NULL_RATE`, `DISTINCT`, `VALUES`, `JOIN`, `PRIMARY_KEY`, `FRESHNESS`, `TYPE_NOTE`, `SOFT_DELETE`, `PII`, `DESCRIPTION`, `USE_CASES`, `ENTITY_TYPE`, `PITFALL`, `GLOSSARY`, `METRIC`, `NEVER_QUERY`, `WAYPOINT_RULE` (an open registry: [add your own](../extending/fact-kind.md)) |
| `value` | the kind's payload; a field that is not known is `None`, never a default |
| `provenance` | where it came from (below) |
| `status` | how far it is established (below) |
| `method` | how a measured number was measured: `exact`, or `sampled:<method>:<fraction>:<seed>` |
| `evidence` | the FactLang outcome: claim text, verdict, numbers; SQL kept as a template only |
| `source`, `observed_at` | e.g. `overlay:schemaguru.yaml:12`; a timezone-aware UTC time |

The id of a fact is a content hash, so re-measuring a number updates the same fact instead
of piling up copies.

```python
import schemaguru as sg

guru = sg.Guru.connect("duckdb:///warehouse.duckdb")
guru.measure("shop.orders")                    # MEASURED facts
guru.load_overlays("schemaguru.yaml")          # HUMAN facts
orders = guru.table_ref("orders")              # bare names resolve when unambiguous
for f in guru.store.facts_for([orders], ["ROW_COUNT", "SOFT_DELETE"]):
    print(f.kind.name, f.provenance.value, f.status.value, f.method, dict(f.value))
```

## Provenance

| Provenance | Meaning | Typical facts |
|---|---|---|
| `CATALOG` | read from the warehouse's system catalog | columns, types, declared keys, statistics |
| `DECLARED` | declared by metadata outside the warehouse | dbt descriptions and tests |
| `MEASURED` | computed by a FactLang probe on the data | match rates, null rates, observed values |
| `DRAFTED` | written by an LLM; always marked *(draft)* until checked or accepted | descriptions, pitfalls, soft-delete guesses |
| `HUMAN` | written by a person in an overlay, or an accepted draft | meaning, expected values, rules |

## Status

| Status | Meaning | Rendered as |
|---|---|---|
| `OBSERVED` | a measured number (no predicate to pass) | `measured <date>` |
| `VERIFIED` | a FactLang claim passed | `verified <date>` |
| `CONTRADICTED` | a claim failed: the data disagrees | a `⚠` warning |
| `UNVERIFIED` | drafted or declared, not checked (yet, or not checkable) | `(draft)`, `(human, unchecked)` |
| `ESTIMATED` | measured on a sample of a large table | `≈ 3.2% (sampled 1%, ±0.4%)` — never verified |
| `UNMEASURABLE` | nothing to measure, e.g. an empty table | `unmeasurable: …` |
| `ERROR` | the probe failed, or was over budget | the reason |
| `STALE` | observed or verified, but older than the TTL (14 days) | still shown, flagged |
| `WAIVED` | a contradiction a person acknowledged, with a mandatory expiry | `WAIVED until <date> (<reason>)` |

## Precedence: who wins

The effective knowledge for each subject and kind is computed by one deterministic rule
(`schemaguru.knowledge.resolve`):

1. **Meaning** (description, use cases, entity type, pitfalls, glossary): `HUMAN` beats
   `DRAFTED`, always.
2. **Expectations** (values, joins, primary keys, soft delete) written by a human or declared
   by metadata are **never replaced by measurement**. They are checked; the view shows the
   expectation *and* the contradiction ("declared values a,b — observed also c (412 rows)").
3. **Structure** (does the column exist, what is its type): the catalog wins; a human note
   that disagrees is kept and the disagreement is shown.
4. Among measured facts, the newest wins; the worst verdict wins for warnings.
5. Drafts are shown with a *(draft)* marker unless their checkable part was verified.

## Joins have a direction

A `JOIN` fact is directed child → parent (`orders.customer_id → customers.customer_id`) and
measured both ways in one probe:

* **match rate** — the share of distinct child keys found in the parent (forward);
* **coverage** — the share of distinct parent keys reached by the child (reverse);
* **fan-in** — child rows per parent key; **cardinality** `N:1`, `1:1`, `1:N` or `N:M`;
* **verdict** — `OK` (match ≥ 95%), `WEAK` (≥ 5%) or `BROKEN` (below 5%; refused).

A join can be perfect forward and nearly empty backward (in the shop, `payments → orders`
matches 100% but covers 5% of orders); the two are different facts and are never mixed.

## Lazy and read-only

Nothing about an agent's question writes knowledge or spends warehouse money by default.
Measurement, drafting and refresh are explicit runs; the question path only *queues* what it
found unmeasured. See [Measure](measure.md) and [Security](security.md).

```python
guru.close()
```
