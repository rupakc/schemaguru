# Rendering

> Rendering turns resolved knowledge into the text an agent reads: a legend, glossary hits, one block per table and the safe join paths between the shown tables.
> Warnings come first, every number carries its provenance and date, drafts are marked, thin joins state their coverage, and the legend says that the absence of a warning is not verification.
> A view is byte-deterministic, carries a fingerprint, and comes in four profiles (agent, planner, verifier, router) and four formats (text, markdown, json, xml).

## A complete view

`guru.context("refund rate by month")` on the shop fixture after crawling, measuring and loading
the spec example overlay. Real output (SchemaGuru 0.1.0, 2026-11-24; measured numbers from the
test suite's stand-in backend):

```text
Legend:
  ⚠ a hazard: measured, declared by a person, or drafted (then marked (draft)).
  ≈ / ≥ estimated from a sample of a large table; never verified.
  (draft) written by a language model and not checked.
  measured <date> an observed number; verified <date> a claim that passed its check.
  (human, unchecked) / (declared, unchecked) stated by a person or a declaration; not yet checked against the data.
  Absence of a warning is not verification.

Glossary:
  active customer: ordered in the last 90 days [tables: shop.orders, shop.customers]

[shop.orders] One row per order placed on the storefront.
  ⚠ Pitfall: Refunds are a second row with negative amount. — fix: Filter amount > 0 for gross revenue.
  ⚠ WAIVED until 2027-01-31 (legacy upper-case rows being migrated): status: declared values 'paid','refunded','cancelled' — observed also 'PAID' (412 rows)
  ⚠ status: case drift: 'paid'/'PAID' differ only in case — compare case-insensitively
  ⚠ created_at: stored as VARCHAR, holds ISO-8601 date text (100% of values, measured 2026-11-24) — CAST before comparing
  Answers: revenue by month; average basket size
  10,000 rows (measured 2026-11-24)
  live rows: (deleted IS NULL OR NOT deleted) (human, unchecked)
  Columns:
    order_id     INTEGER  PK (unique, measured 2026-11-24); ← shop.order_items.order_id (coverage 100%), ← shop.payments.order_id (coverage 5%); distinct 10,000 (measured 2026-11-24)
    customer_id  INTEGER  → shop.customers.customer_id N:1, match 98.04% (measured 2026-11-24); distinct 1,020 (measured 2026-11-24)
    account_id   INTEGER  → shop.accounts.account_id N:1, match 100%, coverage 40% (measured 2026-11-24); distinct 40 (measured 2026-11-24)
    status       VARCHAR  values: 'paid','refunded','cancelled' (+ observed 'PAID'); distinct 4 (measured 2026-11-24); Payment state of the order.
    created_at   VARCHAR  text holding ISO-8601 date text (see warning); note: ISO-8601 timestamp stored as STRING (human, unchecked); distinct 10,000 (measured 2026-11-24)
    deleted      BOOLEAN  values: TRUE (300) (measured 2026-11-24); null 97% (measured 2026-11-24); distinct 1 (measured 2026-11-24)
  Provenance: human overlay · measured 2026-11-24

Not shown: no other candidate tables.
```

Reading it from the top:

- **Legend** appears only when the view contains a `⚠`, and always ends with "Absence of a warning
  is not verification."
- **Glossary** entries matching the question.
- **The table block**: hazards first (`⚠` lines: pitfalls, waived contradictions, case drift, type
  notes), then what the table answers, the row count with its source and date, the live-rows
  predicate, then columns. Each column line gives keys and joins (`→` to a parent with match rate,
  `←` from children with coverage), values (declared and observed), null rate and distinct count,
  each with where it came from.
- **Provenance** line: what kinds of sources this block rests on.
- **Not shown**: how many other candidates exist.

Rules the renderer follows:

- **Consequence order**: warnings before confirmations; truncation drops confirmations, never
  warnings; hedges sit above what they qualify.
- **Numbers as numbers**: a thin join states its coverage (`coverage 5%`), never just "weak".
- **Value lists are capped** at `scope.max_values_shown` (6) with "+N more".
- **Provenance on every fact**: `measured <date>`, `verified <date>`, `(draft)`,
  `(human, unchecked)`, `(declared, unchecked)`, `catalog stats`, `≈ ... (sampled 1%, ...)`.
- **Unknown stays unknown**: "unmeasured", "unmeasurable: no left keys", never a made-up number.
- **Value policy**: every profile ends with the outbound shape sweep ([[PII-and-Sampling-Policy]]).

More real table blocks from the same run (via the `describe_table` tool):

```text
[shop.customers]
  1,000 rows (measured 2026-11-24)
  Columns:
    customer_id  INTEGER  PK (unique, measured 2026-11-24); ← shop.orders.customer_id (coverage 100%); distinct 1,000 (measured 2026-11-24)
    email        VARCHAR  distinct 1,000 (measured 2026-11-24)
    phone        VARCHAR  null 10% (measured 2026-11-24); distinct 900 (measured 2026-11-24)
    segment      VARCHAR  values: 'retail' (334), 'vip' (333), 'wholesale' (333) (measured 2026-11-24); distinct 3 (measured 2026-11-24)
  Provenance: measured 2026-11-24
```

```text
[shop.empty_t]
  0 rows (measured 2026-11-24)
  Columns:
    id  INTEGER  → shop.ref_parent.id (unmeasurable: no left keys)
    v   VARCHAR
  Provenance: measured 2026-11-24
[shop.all_null_t]
  ⚠ c: always NULL (dead column, measured 2026-11-24)
  100 rows (measured 2026-11-24)
  Columns:
    id  INTEGER  → shop.ref_parent.id 1:1, match 100%, coverage 10% (measured 2026-11-24); distinct 100 (measured 2026-11-24)
    c   INTEGER  distinct 0 (measured 2026-11-24)
  Provenance: measured 2026-11-24
```

Note `empty_t` (0 rows: its join is "unmeasurable", not 0%) versus `all_null_t.c` (100 rows, all
NULL: a dead-column warning).

## Profiles

| Profile | For | Shape |
|---|---|---|
| `agent` (default) | an agent that writes SQL | the full block above |
| `planner` | a planning step | tables, use cases, keys and join targets, few columns, plus join paths |
| `verifier` | a checking step or a parser | keyed lines: `TABLE`, `WARN`, `PITFALL`, `ROWS`, `PK`, `COLUMNS`, `VALUES`, `JOIN`, `NULLS`, `DISTINCT`, `TYPE`... |
| `router` | classifying a question | one line per table: name and description |

Choose one with `guru.context(q, profile="planner")` or `schemaguru context "..." --profile
planner`. Real `router` output for "revenue by customer segment from paid orders":

```text
Glossary:
  active customer: ordered in the last 90 days [tables: shop.orders, shop.customers]

shop.orders — One row per order placed on the storefront.
shop.customers
shop.order_items
shop.payments

Not shown: no other candidate tables.
```

And the start of the `verifier` output for the same question:

```text
TABLE shop.orders
PITFALL ⚠ warn: Refunds are a second row with negative amount. — fix: Filter amount > 0 for gross revenue.
WARN ⚠ WAIVED until 2027-01-31 (legacy upper-case rows being migrated): status: declared values 'paid','refunded','cancelled' — observed also 'PAID' (412 rows)
WARN ⚠ status: case drift: 'paid'/'PAID' differ only in case — compare case-insensitively
WARN ⚠ created_at: stored as VARCHAR, holds ISO-8601 date text (100% of values, measured 2026-11-24) — CAST before comparing
ROWS 10000 measured 2026-11-24
PK order_id unique measured 2026-11-24
SOFT_DELETE (deleted IS NULL OR NOT deleted) human unchecked
COLUMNS order_id INTEGER | customer_id INTEGER | account_id INTEGER | status VARCHAR | created_at VARCHAR | deleted BOOLEAN
...
```

Profiles are an open set: [[Extending-Profile-and-Format]].

## Detail levels

When the budget is tight a table is rendered at a lower level: `full` → `standard` → `lite` (at
most 8 priority columns) → `name-only`. Warnings survive every level that has room for them.
[[Scoping]] explains the fitting.

## Join paths in a view

When several tables are shown, the view lists the safe join route between them, or a refusal.
From `guru.context("how many events per account and session")` (real output, abbreviated to the
paths block):

```text
Join paths (join keys, advisory; support = the weakest hop's match or coverage):
  ⚠ no safe route (shop.events -> shop.orders; shop.sessions -> shop.orders): every route pivots through hub shop.accounts on two attribution edges (fan-in > 10): a cartesian hazard
  shop.events → shop.sessions · support 100%
    events.session_id = sessions.session_id (N:1)
  shop.events → shop.accounts · support 100%
    events.account_id = accounts.account_id (N:1)
  shop.accounts → shop.orders · support 40%
    orders.account_id = accounts.account_id (N:1)
```

## The `ContextView` object

| Member | What |
|---|---|
| `.text` | the view as plain text |
| `str(view)` | the view in the format it was asked for (`format=`, default text) |
| `.to_markdown()`, `.to_json()`, `.to_xml()` | other formats (`json` is canonical: sorted keys, compact) |
| `.blocks` | ordered `ViewBlock`s; `stable=True` blocks (global pitfalls, never-query, legend) come first and are the same for every question, so a host can put a prompt-cache breakpoint after them |
| `.tables` | the tables shown |
| `.fingerprint` | `sha256:` of the text |
| `.tokens` | the token count by the configured counter |
| `.diagnostics` | findings from rendering (`SG50x`) |

`guru.context(q, format="markdown")` returns a view whose `str()` is markdown (`.text` stays plain text). The format set is
open (`sg.render.register_format`).

If even the always-on blocks do not fit the budget, `SG504` names what overflowed. Info `SG505`
reports how many values or text spans the value policy masked.

## Fingerprints

`guru.fingerprint_views(questions)` renders a fixed question set and returns their fingerprints,
so a host's CI can notice when a refresh changed what the agent will read. Real output:

```text
{'refund rate by month': 'sha256:30d46be50d1bf33ff522248f80c8773c3c9ac0c12ab92496b4966c111e33e93c', 'revenue by customer segment': 'sha256:67ea9a3a666a53a59224a9bdfffd805c4590e7981014cd02be94db54869aa5bd'}
```

Store the dict, re-run after each refresh, and diff.

## See also

- [[Scoping]] - which tables reach the renderer
- [[Concepts]] - the statuses behind each marker
- [[Agent-Integration]] - views as prompt text and tool results
- [[Extending-Profile-and-Format]]
