# Store

> The store is where SchemaGuru keeps everything it learns: catalog structure, facts, snapshots, the measurement queue, the spend ledger and feedback.
> The default is one SQLite file per warehouse (`./.schemaguru/<name>.db`) with full-text search built in; a shared Postgres store is available for teams and server mode.
> All writes are idempotent upserts keyed on stable ids, and exports are byte-deterministic so you can commit and diff them.

## Default: `SQLiteStore`

```python
guru = sg.Guru.connect("duckdb:///warehouse.duckdb", name="shop")   # store: ./.schemaguru/shop.db
guru = sg.Guru.connect(url, store="/data/schemaguru/prod.db")       # explicit path
```

The path is chosen in this order: the `store=` argument, `Config.store_path`, then
`.schemaguru/<name>.db` (with `name="default"` unless you pass one).

Properties:

- **One file, WAL mode, one connection** guarded by a re-entrant lock; a busy timeout of 30 s.
  Concurrent readers in the same process are safe; measurement writes go through the same
  connection.
- **Full-text search** with SQLite FTS5 (BM25 over table name tokens, column names, comments,
  descriptions and use cases). If your Python's SQLite lacks FTS5, search falls back to a slower
  `LIKE` scan and diagnostic `SG701` is recorded. `schemaguru doctor` tells you which you have.
- **Migrations** are numbered SQL files in `store/migrations/` (`0001_initial.sql`,
  `0002_scope.sql`), applied on open. The store records its schema version; opening a store
  written by a newer SchemaGuru with an older one is refused with `SG702`, so an old library can
  never silently corrupt a newer file.

## What is in it

| Table | Holds |
|---|---|
| `schemas`, `tables`, `columns`, `constraints` | the crawled structure (withdrawn tables keep a `withdrawn_at`) |
| `facts` | every fact, keyed by `Fact.id` |
| `docs` + FTS index `table_docs` | the searchable text of each table |
| `snapshots`, `snapshot_facts` | frozen copies of the facts at each refresh, plus which checks ran |
| `crawl_state` | the crawl checkpoint and the last good crawl |
| `ledger` | every byte, token and query spent (`crawl`, `measure`, `draft`, `agent_check`) |
| `queue` | tables waiting to be measured, with a reason and priority |
| `feedback` | failed-query records: table, column, failure class, count (no SQL text) |
| `table_embeddings` | optional vectors for the embedding retriever ([[Scoping]]) |

`schemaguru report` summarises it:

```text
14 live table(s), 0 withdrawn
0 fact(s); 0 draft(s) awaiting review
latest snapshot: None
spend: measure_bytes 0, measure_queries 0, draft_tokens 0, crawl_queries 6
0 failing-query pattern(s) reported
```

(Real output after `schemaguru crawl` on the shop fixture, 2026-11-24.)

## Export and import

```python
guru.export("shop-knowledge.json")
```

The export holds schemas, tables (with columns and constraints, withdrawn ones included) and
facts. It is sorted and byte-deterministic: equal knowledge gives an identical file, so a
snapshot can be committed to git and reviewed as a diff. Operational state (snapshots, crawl
state, ledger, queue, feedback) is not exported. `schemaguru.store.export.import_json(store,
path)` loads an export into another store. Both stream one table at a time.

## The `Store` protocol

Anything that implements `schemaguru.store.protocol.Store` can be passed as `Guru(store=...)`.
The protocol is CRUD over the model plus `search`, `resolve`, `facts_for`, snapshots, crawl
state, ledger, queue, feedback and `transaction()`. See [[Extending-Store]].

## Shared: `PostgresStore`

For teams, workers on several machines and [[Server]] mode (extra `postgres-store`, driver
psycopg 3):

```python
from schemaguru.store.postgres import PostgresStore
store = PostgresStore("postgresql://user@db-host/schemaguru", schema="schemaguru")   # schema optional
guru = sg.Guru.connect(url, store=store)
```

or `--store postgres://...` on any CLI command. Same model and contract as the SQLite store, so
processes can share one store:

- **Connections**: a bounded pool (`max_connections=8`, `acquire_timeout_s=30`); a transaction pins
  one connection to the calling thread.
- **Migrations**: their own numbered series (`store/migrations/pg/`), applied on open under an
  advisory lock, so replicas starting together migrate once; a newer schema is refused (`SG702`).
- **Search**: a weighted `tsvector` per table (name tokens, then column names, then comments and
  descriptions) with prefix matching; table-name hits always rank first, as in SQLite, though the
  order *within* a field can differ from SQLite's BM25.
- **Queue**: `queue_pop` claims rows with `FOR UPDATE SKIP LOCKED`, so concurrent workers never
  take the same table.
- **Embeddings** are stored as bytes; `pgvector` is not required.
- `advisory_lock("refresh")` lets the server run one refresh at a time across replicas.
- A lost or refused connection, or an exhausted pool, raises `StoreUnavailableError` (`SG703`).

It is tested against a real PostgreSQL 16 server in the CI integration job, and both stores pass
one shared contract suite
([`tests/unit/store/contract.py`](https://github.com/rupakc/schemaguru/blob/main/tests/unit/store/contract.py)).

## Practical notes

- The store is a cache of warehouse knowledge plus your human overlays' facts. The overlays
  themselves stay in your YAML files, so you can delete the store and rebuild it with a crawl,
  a measurement run and `load_overlays`.
- Keep `.schemaguru/` out of version control (it holds measured values, masked by default, and
  spend history). Commit the JSON export instead if you want a reviewable record.
- Use one store file per warehouse: give each `Guru` its own `name` (or `store=` path).

## See also

- [[Catalog-and-Crawl]] - what fills the structure tables
- [[Refresh-and-Drift]] - snapshots and diffs
- [[Extending-Store]] - writing another backend
- [[Security]] - what the store contains and how to protect it
