# Scoping

A warehouse with 100,000 tables does not fit in a prompt, and dumping the schema is the
wrong default even when it does. `guru.context(question, budget_tokens=4000)` picks the
slice a question needs and fits it into the budget.

## The pipeline

1. **Seeds.** The retriever ranks tables for the question (`ScopeConfig.seed_k`, 8). The
   default `LexicalRetriever` is the store's full-text search (SQLite FTS5, BM25) over name
   tokens (snake_case and camelCase split), column names, comments, descriptions, use cases,
   pitfalls and the glossary terms mapped to the table, plus a boost for a table named
   verbatim in the question (`shop.orders` or `orders`).
2. **Glossary.** A glossary term or metric whose words all occur in the question pulls in its
   mapped tables.
3. **Bridges.** The best measured join path between each pair of seeds is added, at most
   `max_expansion_tables` (6) extra tables, so the agent sees the tables that connect its
   seeds — and the refusals between them.
4. **Fit.** Tables are ranked (seed score, then bridges) and each is rendered at the richest
   detail level that still fits: `full` → `standard` → `lite` (columns by priority: keys,
   join columns, columns named by pitfalls or soft-delete rules, columns with descriptions
   or values, then the rest) → `name-only`. Warnings are kept before confirmations at every
   level.
5. **Always-on blocks.** Global pitfalls, the never-query list and the legend, within budget.
6. The view ends with a **"Not shown"** line counting the other candidate tables, and the
   agent tools (`search_tables`, `describe_table`) fetch more when needed.

```python
import schemaguru as sg

guru = sg.Guru.connect("duckdb:///warehouse.duckdb")
guru.measure(all=True)
guru.load_overlays("schemaguru.yaml")
for budget in (4000, 600, 250):
    view = guru.context("revenue per customer from paid orders", budget_tokens=budget)
    print(budget, "->", view.tokens, "tokens,", [t.table for t in view.tables])
print(guru.context("which active customer ordered", budget_tokens=600).text[-300:])
```

When even the always-on blocks do not fit, the view says so (`SG504`) rather than silently
dropping a warning. Tokens are counted by a pluggable counter: the default is a
characters-divided-by-four heuristic; `Config(token_counter="tiktoken")` (extra `tiktoken`)
counts real tokens, or pass `token_counter=` to `Guru` ([extending](../extending/token-counter.md)).

## Deterministic and cached

The same store snapshot, question and configuration render a **byte-identical** view; table
order never depends on dict order or timing. Views are cached in an LRU
(`ScopeConfig.cache_entries`, 256) keyed on the snapshot, the question, the configuration and
the day; every write through the `Guru` invalidates the cache.

```python
a = guru.context("refund rate by month")
b = guru.context("refund rate by month")
print(a.fingerprint == b.fingerprint, a.fingerprint[:16])
guru.close()
```

## Scale

Search runs inside the store (the FTS index), never as a Python scan over every table: on a
synthetic 500,000-table store the search p95 was about 167 ms on the developer's machine
(hardware-dependent); the whole `context` call at that size is far slower (p95 28.6 s, a
documented known limit, see [Scale](scale.md)). If the local SQLite lacks FTS5, search falls back to a slower `LIKE`
scan with a warning (`SG701`). The join graph is built from the store's join facts and
cached until the knowledge changes.

## Other retrievers

`HybridRetriever(*retrievers)` fuses rankings by reciprocal rank;
`EmbeddingRetriever(store, embedder)` (extra `embeddings`) ranks by cosine similarity with a
pluggable `Embedder`. Pass one as `Guru(retriever=...)`; see
[Retriever and embedder](../extending/retriever.md).

## First touch

A scoped table that was never measured is marked *unmeasured* in the view and queued for
measurement; the view is not blocked on it unless `measure_on_demand="block"` (capped by
`block_timeout_s`). See [Measure](measure.md#lazy-by-default).
