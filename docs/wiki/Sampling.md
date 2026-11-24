# Sampling

> Small tables are measured exactly; tables too large to read in full (by default 50 GiB by catalog bytes, or, only when bytes are unknown, 500 million rows) are measured on a representative sample.
> Joins and keys use key-hash sampling (the same keys on both sides), column profiles use block sampling, and `LIMIT` is never a sampling method.
> A sampled number is always labelled as an estimate with its fraction and interval, is never compared with an exact number, and can refute an expectation but never confirm one.

This page is about sampling *measurement*. What an LLM may see of your data values is a separate
setting, covered in [[PII-and-Sampling-Policy]].

## When a table is sampled

A table is **large** when the catalog says `bytes ≥ sampling.sample_threshold_bytes` (50 GiB), or,
when bytes are unknown, `row_count ≥ sampling.sample_threshold_rows` (500,000,000). A table with
neither statistic is not large. Everything that is not large is measured exactly.

`sampling.measurement` overrides the rule:

| Value | Behaviour |
|---|---|
| `auto` (default) | sample large tables only |
| `off` | always exact; probes over the per-probe budget become `ERROR over_budget` (`SG301`) |
| `force` | sample every table (useful for tests and demos) |

## How each probe is sampled

| Probe | Method | Why |
|---|---|---|
| join overlap, uniqueness, fan-out, distinct keys | **key-hash** (`keyhash`) | keep rows whose hashed key falls in a bucket range. All rows of a kept key are kept, so per-key counts are exact for those keys. The same hash, seed and range on both sides of a join keep exactly the same keys, so match rate and coverage are unbiased. Used only when the key has more than `exact_key_limit` (5,000,000) distinct values; the fraction targets about that many sampled keys. |
| null rates, pattern rates | **block** (`system`), or row-level `bernoulli` where the dialect has no block sampling | block sampling is what actually reduces bytes billed. The fraction is chosen so the probe fits `max_bytes_per_probe`, clamped to 0.1%..10% (`block_fraction_floor`, `block_fraction_ceiling`). |
| top values (enum discovery) | **key-hash on the table's primary key**, at the block fraction | FactLang refuses block/Bernoulli samples for probes that read a table more than once (FactLang error `FL405`). With no known primary key, top values are measured exactly, and the per-probe budget refuses an over-budget probe (`SG301`). |
| freshness (`MAX` of a timestamp), row counts, every claim | **never sampled** | a maximum must be exact; row counts come from catalog stats when the reader has them, else an exact `COUNT(*)`, else `ERROR over_budget`. Claims are verdicts and are never sampled. |

**A cost caveat for column-billed warehouses.** Key-hash sampling still scans the key columns in
full (the hash has to be computed for every row). On BigQuery, which bills by columns read,
key-hash sampling of top values on the primary key does not reduce bytes scanned, so a huge
table may still be over budget (`SG301`). Raise `max_bytes_per_probe` for that table's run, or
accept that its enum values stay unmeasured.

## What a sampled number means

- **Ratios** (null rate, match rate, coverage, pattern rate) get a Wilson 95% interval on the
  sampled rows. Block samples are clustered, so the interval is widened by a design-effect factor
  (`design_effect`, 2.0) and labelled "approximate".
- **Counts** are scaled by 1/fraction and labelled.
- **Distinct counts and maxima** from a block or Bernoulli sample are **lower bounds** (`≥`).
- **Observed values** from a sample are "values seen in sample"; a rare value that was not seen
  is *not* claimed absent.
- The fact's status is `ESTIMATED` and its method is
  `sampled:<keyhash|system|bernoulli>:<fraction>:<seed>`. The seed is fixed per (table, snapshot)
  so re-runs reproduce the same sample.

## Refute, never confirm

A sample can **refute**: a declared enum with an undeclared value seen in the sample is
`CONTRADICTED` (the value exists; the evidence is attached), and a declared-unique key with a
duplicate inside a key-hash sample is `CONTRADICTED`. A sample can never **confirm**: no
counter-example leaves the fact `ESTIMATED` ("consistent with the sample"), never `VERIFIED`.

To confirm exactly, ask for it explicitly, within budget:

```python
estimated = [f for f in guru.store.iter_facts() if f.status is sg.Status.ESTIMATED]
exact = guru.verify(estimated, exact=True)     # re-measures those tables with sampling off
```

## What it looks like

Real output: the shop's `orders` table measured with `Config(sampling=SamplingConfig(measurement="force"))`
(the table is small, so `force` was needed; generated 2026-11-24 with SchemaGuru 0.1.0 and the
test suite's stand-in backend, which renders block sampling as DuckDB Bernoulli sampling):

```text
[shop.orders]
  sampled table: ≈ and ≥ figures are estimates from a sample
  10,050 rows (catalog stats 2026-11-26)
  Columns:
    order_id     INTEGER  PK (no duplicate in sample (sampled 0.1%)); ← shop.payments.order_id (coverage ≈ 11.11% (sampled 0.1%, 1.989% to 43.5%)); values seen in sample: 323, 1163, 2532, 2692, 2745, 3949 +8 more (sampled 0.1%; rarer values may be missing); null ≈ 0% (sampled 0.1%, 0% to 48.99%, approximate); distinct ≈ 14,000 (sampled 0.1%)
    customer_id  INTEGER  → shop.customers.customer_id N:1, match ≈ 100% (sampled 0.1%, 20.65% to 100%); values seen in sample: 38, 260, 302, 374, 384, 397 +8 more (sampled 0.1%; rarer values may be missing); null ≈ 0% (sampled 0.1%, 0% to 48.99%, approximate); distinct ≥ 8 (sampled 0.1%, lower bound)
    account_id   INTEGER  → shop.accounts.account_id (unmeasurable: no left keys); values seen in sample: 3, 12, 13, 25, 2, 11 +4 more (sampled 0.1%; rarer values may be missing); null ≈ 0% (sampled 0.1%, 0% to 48.99%, approximate); distinct ≥ 8 (sampled 0.1%, lower bound)
    status       VARCHAR  values seen in sample: 'paid', 'cancelled', 'refunded' (sampled 0.1%; rarer values may be missing); null ≈ 0% (sampled 0.1%, 0% to 48.99%, approximate); distinct ≥ 4 (sampled 0.1%, lower bound)
    created_at   VARCHAR  values seen in sample: '2025-01-14T11:00:00Z', '2025-02-18T11:00:00Z', '2025-04-16T12:00:00Z', '2025-04-23T04:00:00Z', '2025-04-25T09:00:00Z', '2025-06-14T13:00:00Z' +8 more (sampled 0.1%; rarer values may be missing); null ≈ 0% (sampled 0.1%, 0% to 48.99%, approximate); looks like ISO-8601 date text: ≈ 100% (sampled 0.1%, 51% to 100%, approximate) of values; distinct ≥ 8 (sampled 0.1%, lower bound)
    deleted      BOOLEAN  null ≈ 100% (sampled 0.1%, 51% to 100%, approximate); distinct ≥ 0 (sampled 0.1%, lower bound)
  Provenance: measured 2026-11-26 · sampled 0.1%
```

Read it the way an agent should: a 0.1% sample of a 10,000-row table is about ten rows, so every
interval is wide, the `PAID` case-drift value was not seen (and is not claimed absent), and the
coverage estimate (≈ 11%, interval 2% to 44%) brackets the exact 5% only loosely. On a real
terabyte table the sample holds millions of rows and the intervals are narrow. Note also that no
`⚠` line here comes from the sample: an estimate never produces a "verified" confirmation.

(The "+8 more" lists show values; on a column the value policy masks, they would be shape tokens.)

## Configuration summary

All in `sg.SamplingConfig` (see [[Configuration]]):

| Field | Default |
|---|---|
| `measurement` | `"auto"` |
| `sample_threshold_bytes` | `50 << 30` (50 GiB) |
| `sample_threshold_rows` | `500_000_000` |
| `exact_key_limit` | `5_000_000` |
| `block_fraction_floor` | `0.001` |
| `block_fraction_ceiling` | `0.10` |
| `design_effect` | `2.0` |

## See also

- [[Measurement]] - the probes being sampled
- [[Scale-and-Cost]] - when sampling saves money and when it does not
- [[Rendering]] - how `≈` and `≥` appear in views
- [[Design-Decisions]] - why `LIMIT` is never a sample
