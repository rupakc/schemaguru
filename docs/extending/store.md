# Store

The store holds everything SchemaGuru knows: structure, facts, snapshots, the crawl
checkpoint, the budget ledger and the feedback queue. Two backends ship:

* `SQLiteStore(path)` — the default; one file, WAL mode, FTS5 search (a `LIKE` fallback with
  `SG701` where FTS5 is missing).
* `PostgresStore(dsn, schema=None)` (extra `postgres-store`) — the same schema, weighted
  `tsvector` search; for teams and [server mode](../guide/server.md).

Migrations are numbered SQL files applied on open; a store written by a newer schema
version than the library knows is refused (`SG702`).

## Protocol

`schemaguru.store.Store` (about 30 methods) covers CRUD over the model plus `search(query, k)`,
`facts_for(subjects, kinds)`, `put_facts`, snapshots and diffs, `crawl_state`, the budget
ledger, the feedback queue and transactions. Every write is an idempotent upsert keyed on
stable ids; every listing is in a stable order. Read the protocol's docstrings in
`schemaguru/store/protocol.py` before implementing it.

## Plugging one in

Any object implementing the protocol can be passed as `Guru(store=...)`; a `str` or path
opens a `SQLiteStore`. A thin wrapper is the easiest worked example — here one that counts
fact writes (for metrics) and delegates the rest:

```python
import schemaguru as sg
from schemaguru.store import SQLiteStore


class CountingStore:
    def __init__(self, inner):
        self.inner, self.fact_writes = inner, 0

    def put_facts(self, facts):
        facts = list(facts)
        self.fact_writes += len(facts)
        return self.inner.put_facts(facts)

    def __getattr__(self, name):          # everything else: delegate
        return getattr(self.inner, name)


store = CountingStore(SQLiteStore(".schemaguru/counted.db"))
guru = sg.Guru.connect("duckdb:///warehouse.duckdb", store=store)
guru.measure("shop.orders")
guru.load_overlays("schemaguru.yaml")
print(store.fact_writes, "facts written")
guru.close()
store.inner.close()
```

A `Guru` closes only a store it opened itself; close yours when you are done.

## Test template

`tests/unit/store/contract.py` is the backend-agnostic contract: every function in it is a
test taking a `store` fixture. Run it against your backend:

```py
# tests/test_my_store.py
import pytest

from tests.unit.store.contract import *  # noqa: F403  (the contract tests)


@pytest.fixture
def store(tmp_path):
    s = MyStore(tmp_path / "k.db")
    yield s
    s.close()
```

The SQLite and Postgres backends pass the same suite (`tests/unit/store/test_sqlite.py`,
`tests/integration/store/test_postgres_store.py`).
