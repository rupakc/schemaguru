# Scoping

> Scoping picks the slice of the warehouse a question is about: the best-matching tables (seeds), the tables on the best join paths between them (bridges), and nothing else.
> Each table is then rendered at the richest detail level that still fits the token budget, degrading detail before dropping tables.
> The same store, question and config always give byte-identical output, and the view ends by saying how many other candidates were not shown.

## The pipeline behind `guru.context(question, budget_tokens=4000)`

1. **Retrieve seeds.** The retriever returns up to `scope.seed_k` (8) tables for the question.
2. **Glossary and metric hits.** A glossary term or metric whose words all occur in the question
   pulls in its `maps_to` tables.
3. **Graph expansion.** For each pair of seeds, the single best join path is computed and the
   tables in between are added as bridges, at most `scope.max_expansion_tables` (6), cheapest
   first. Refused pairs (hub pivot, no route) add nothing.
4. **Budget fit.** Tables are ranked (seeds by score, then bridges by path cost) and each gets the
   richest *detail level* that leaves room for the rest:
   `full` → `standard` → `lite` → `name-only`. Every remaining table is reserved its `name-only`
   cost first, so detail degrades before tables disappear; a table is dropped (from the bottom)
   only when even the names would not fit. `lite` keeps at most 8 columns, chosen by priority:
   primary key, join columns, columns named in pitfalls or the soft-delete rule, columns with
   descriptions or values, then the rest.
5. **Always-on blocks**: global pitfalls, the never-query list and the legend, reserved up front.
6. A `ContextView` is returned ([[Rendering]]).

## Retrievers

| Retriever | Extra | How it ranks |
|---|---|---|
| `LexicalRetriever` (default) | none | the store's full-text index (FTS5 BM25) over table name tokens (snake_case and camelCase split), column names, comments, descriptions, use cases and pitfalls. A table named verbatim in the question (`shop.orders`, or just `orders`) has its score doubled (`IDENTIFIER_BOOST = 2.0`); if text search missed it, it enters at the top score. |
| `EmbeddingRetriever(store, embedder)` | `embeddings` | cosine similarity between the question's vector and each table document's vector; vectors are stored per `(model, table)` with a hash of the document, so only changed tables are re-embedded |
| `HybridRetriever(*retrievers, k_rrf=60)` | none | reciprocal-rank fusion of several retrievers |

```python
from schemaguru.scope import EmbeddingRetriever, HybridRetriever, LexicalRetriever
lexical = LexicalRetriever(guru.store)
hybrid = HybridRetriever(lexical, EmbeddingRetriever(guru.store, my_embedder))
guru.retriever = hybrid      # or pass retriever= to sg.Guru(...)
```

Assign the retriever before the first `context()` call: views already in the cache are not
invalidated by the assignment.

Bring your own embedder: [[Extending-Retriever-and-Embedder]].

### Retrieval only knows what is written down

Lexical retrieval matches words. On a store that was only crawled, the only words are table and
column names, so a question that uses business words the names do not contain finds nothing.
Real CLI output on the crawled, unmeasured, un-described shop fixture:

```text
$ schemaguru context refund rate by month
Not shown: no other candidate tables.
[exit 0]
```

"refund" appears in no table or column name. The same question after loading the spec example
overlay (whose `orders` pitfall mentions refunds and whose use cases include "revenue by month")
finds `orders`; see the full view on
[[Rendering]]. Descriptions, use cases and glossary terms (from [[Overlays]] or [[Drafting]]) are
what make retrieval work on business language. A question that names tables works immediately:

```text
$ schemaguru context "orders per customer" --budget-tokens 1500
[shop.orders]
  unmeasured: nothing here has been measured yet; figures are declared or drafted
  10,000 rows (catalog stats)
  Columns:
    order_id     INTEGER  PK (declared, enforced)
    customer_id  INTEGER
    account_id   INTEGER
    status       VARCHAR
    created_at   VARCHAR
    deleted      BOOLEAN
  Provenance: catalog only
...
```

(Real output, truncated after the first table; the other three shown were `customers`,
`order_items` and `payments`.)

## First touch

A table shown in a view that has never been measured is marked "unmeasured" and pushed onto the
measurement queue with reason `first_touch`. The view is never blocked on it unless you set
`measure_on_demand="block"` (then `context()` measures those tables first, for at most
`block_timeout_s`, 10 s). See [[Measurement]].

## Determinism and caching

Same store snapshot + same question + same config ⇒ byte-identical view. Retrievers order ties
by canonical name; expansion searches each seed pair once from the canonically smaller seed. The
façade caches views in an LRU (`scope.cache_entries`, 256) keyed on snapshot, question hash,
config hash, profile, budget and a write counter, so a cached view never outlives a write.

## Recall safety nets

A view always ends with a "Not shown" line counting other candidate tables (among the first
`seed_k + 100` retrieval hits) and scoped tables the budget dropped. The agent can fetch more
with `search_tables` and `describe_table` ([[Agent-Integration]]).

## Scale

Search runs inside the store's FTS index, never as a Python scan over all tables. The scale
sanity test searches a 50,000-table synthetic catalog with a p95 under 200 ms. The nightly
scale benchmark measures the whole `context` call at 500,000 tables, and currently misses its
200 ms p95 target (28.6 s, dominated by join-path search; see [[Scale]]). Graph expansion on a 50,000-edge graph took a median
of about 0.6 s per call in a local run. See [[Scale-and-Cost]].

## See also

- [[Rendering]] - what a table block looks like at each level
- [[Join-Graph]] - where bridges come from
- [[Extending-Retriever-and-Embedder]] and [[Extending-TokenCounter]]
- [[Configuration]] - `ScopeConfig`
