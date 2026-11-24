# Extending: Store

> A `Store` is where knowledge lives; SQLite is the default and `PostgresStore` the shared backend.
> Any object implementing the `schemaguru.store.protocol.Store` protocol can be passed as `sg.Guru(store=...)`.
> Writing a new backend is the largest extension point, so the advice here is to reuse the SQLite store's behaviour as the reference and test against the same expectations.

## The protocol

`schemaguru.store.protocol.Store` covers (all writes are idempotent upserts keyed on stable ids):

| Group | Methods |
|---|---|
| structure | `upsert_schemas`, `upsert_tables`, `upsert_columns`, `upsert_constraints`, `mark_withdrawn`, `table`, `columns`, `constraints`, `iter_schemas`, `iter_tables(include_withdrawn=False)` |
| names | `resolve(text) -> (ref, candidates)`, `tables_named(names, limit=...)` |
| facts | `put_facts`, `iter_facts`, `facts_for(subjects, kinds)` |
| search | `search(query, k)` (lexical, live tables, name hits first) |
| snapshots | `snapshot(label, checks_run=...)`, `snapshot_facts`, `snapshot_checks_run`, `latest_snapshot` |
| state | `crawl_state_get`, `crawl_state_set`, `ledger_add`, `ledger_total`, `queue_push`, `queue_pop`, `feedback_add`, `feedback_summary` |
| transactions | `transaction()` (a context manager) |

The contracts that matter most (each one is relied on elsewhere):

- `iter_tables` streams in canonical-ref order and never loads the whole catalog.
- `upsert_columns` / `upsert_constraints` make the given list the table's *complete* list.
- `mark_withdrawn` is a soft delete that keeps an earlier `withdrawn_at` and touches no facts.
- `resolve`: exact first, then a *unique* case-insensitive match; ambiguous returns
  `(None, candidates)`; never a guess.
- `facts_for(None, ...)` means no filter; an empty iterable matches nothing; results sorted by id.
- `put_facts` upserts by `Fact.id` (re-measurement replaces).
- `search` must run in an index, not as a Python scan (it is on the agent's hot path).
- Optional, for the embedding retriever: `embeddings_put`, `embeddings_get`, `embeddings_hashes`.

## Worked example: wrapping the SQLite store

The cheapest custom store is a wrapper that adds behaviour around `SQLiteStore`, for example
logging every fact write:

```python
from schemaguru.store.sqlite import SQLiteStore

class LoggingStore:
    def __init__(self, path):
        self._inner = SQLiteStore(path)

    def put_facts(self, facts):
        facts = list(facts)
        print(f"writing {len(facts)} facts")
        self._inner.put_facts(facts)

    def __getattr__(self, name):          # everything else: delegate
        return getattr(self._inner, name)

guru = sg.Guru(store=LoggingStore("shop.db"), reader=reader, dialect="duckdb")
```

A store you pass in is not closed by `guru.close()`; close it yourself.

## Test template

The repository has a backend-agnostic contract suite,
[`tests/unit/store/contract.py`](https://github.com/rupakc/schemaguru/blob/main/tests/unit/store/contract.py):
every function in it is a test taking a `store` fixture, and both shipped stores pass it. Run it
against yours:

```python
# tests/test_my_store.py
import pytest
from tests.unit.store.contract import *  # noqa: F403  (the contract tests)

@pytest.fixture
def store(tmp_path):
    s = MyStore(tmp_path / "s.db")
    yield s
    s.close()
```

Two of the contracts, written out as a starting point for backend-specific tests:

```python
def test_resolve_never_guesses(store):
    store.upsert_schemas([Schema(None, "shop")])
    store.upsert_tables([Table(sg.ObjectRef(None, "shop", "Orders"), "table"),
                         Table(sg.ObjectRef(None, "shop", "ORDERS"), "table")])
    ref, cands = store.resolve("shop.orders")
    assert ref is None and len(cands) == 2            # ambiguous: candidates, no guess

def test_put_facts_is_idempotent(store, fact):
    store.put_facts([fact]); store.put_facts([fact])
    assert store.facts_for([fact.subject], [fact.kind.name]) == [fact]
```

## See also

- [[Store]]
- [[Server]] - replicas sharing a Postgres store
- [[Extending-Retriever-and-Embedder]]
- [[Architecture]]
