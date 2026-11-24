from __future__ import annotations

import tracemalloc
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from schemaguru.catalog.crawl import crawl
from schemaguru.config import CrawlConfig
from schemaguru.model.refs import ObjectRef
from schemaguru.model.structure import Column, Constraint, Table
from schemaguru.store.sqlite import SQLiteStore
from tests.unit.catalog.fake_reader import BASE, FakeReader

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SQLiteStore]:
    s = SQLiteStore(tmp_path / "t.db")
    yield s
    s.close()


def live(store: SQLiteStore) -> list[str]:
    return [t.ref.canonical() for t in store.iter_tables()]


def test_crawl_pages_and_checkpoints(store: SQLiteStore) -> None:
    cfg = CrawlConfig(page_size=500, column_batch=100, workers=3)
    reader = FakeReader(5000, tables_per_schema=2500)
    # call 1/2 = schema s0000 pages 1,2; call 3 would be page 3 -> interrupted
    reader.interrupt_at_call = 3
    with pytest.raises(KeyboardInterrupt):
        crawl(reader, store, config=cfg, now=lambda: NOW)
    assert len(live(store)) == 1000
    state = store.crawl_state_get("crawl")
    assert state is not None and state["status"] == "running"
    assert state["cursor"] == {
        "catalog": None,
        "schema": "s0000",
        "page_token": "1000",
        "done": False,
    }

    report = crawl(reader, store, config=cfg, now=lambda: NOW)
    names = live(store)
    assert len(names) == 5000 and len(set(names)) == 5000
    assert report.tables_written == 4000  # the 1,000 already checkpointed are not re-read
    assert report.withdrawn == 0 and not report.errors
    assert store.crawl_state_get("crawl")["status"] == "complete"  # type: ignore[index]
    # every table got its columns and constraints
    ref = ObjectRef(None, "s0001", "t0004999")
    assert [c.ref.column for c in store.columns(ref)] == ["id", "name"]
    assert len(store.constraints(ref)) == 1
    assert max(reader.column_batches) <= 100


def test_resume_false_restarts(store: SQLiteStore) -> None:
    cfg = CrawlConfig(page_size=100)
    reader = FakeReader(300, tables_per_schema=300, page_size=100)
    reader.interrupt_at_call = 2
    with pytest.raises(KeyboardInterrupt):
        crawl(reader, store, config=cfg, now=lambda: NOW)
    report = crawl(reader, store, config=cfg, resume=False, now=lambda: NOW)
    assert report.tables_written == 300


def test_page_error_isolated(store: SQLiteStore) -> None:
    reader = FakeReader(3000, tables_per_schema=1000, fail_schemas=["s0001"])
    report = crawl(reader, store, config=CrawlConfig(), now=lambda: NOW)
    names = live(store)
    assert len(names) == 2000
    assert not any(n.startswith("s0001.") for n in names)
    assert len(report.errors) == 1
    err = report.errors[0]
    assert err.code == "SG102" and "s0001" in err.message and "boom" in err.message
    # a crawl with errors is not a baseline for the next incremental run
    assert store.crawl_state_get("crawl_last") is None


def test_batch_error_isolated(store: SQLiteStore) -> None:
    class Flaky(FakeReader):
        def columns(self, tables: Sequence[ObjectRef]) -> Mapping[ObjectRef, list[Column]]:
            if any(t.table == "t0000000" for t in tables):
                raise RuntimeError("columns down")
            return super().columns(tables)

    reader = Flaky(10, tables_per_schema=10)
    report = crawl(reader, store, config=CrawlConfig(column_batch=5), now=lambda: NOW)
    assert len(live(store)) == 10 and len(report.errors) == 1
    assert store.columns(ObjectRef(None, "s0000", "t0000000")) == []
    assert len(store.columns(ObjectRef(None, "s0000", "t0000009"))) == 2


def test_withdrawn_soft(store: SQLiteStore) -> None:
    reader = FakeReader(20, tables_per_schema=10)
    crawl(reader, store, config=CrawlConfig(), now=lambda: NOW)
    victim = ObjectRef(None, "s0000", "t0000003")
    store.queue_push(victim, "keep", 1.0)
    reader.dropped = {3, 15}
    later = NOW + timedelta(days=1)
    report = crawl(reader, store, config=CrawlConfig(), now=lambda: later)
    assert report.withdrawn == 2 and report.tables_seen == 18
    t = store.table(victim)
    assert t is not None and t.withdrawn_at == later
    assert len(live(store)) == 18
    assert len(store.columns(victim)) == 2  # structure kept
    # revival: the table is back
    reader.dropped = set()
    crawl(reader, store, config=CrawlConfig(), now=lambda: later + timedelta(days=1))
    again = store.table(victim)
    assert again is not None and again.withdrawn_at is None


def test_withdrawn_when_schema_vanishes(store: SQLiteStore) -> None:
    reader = FakeReader(20, tables_per_schema=10)
    crawl(reader, store, config=CrawlConfig(), now=lambda: NOW)
    reader.n_tables = 10  # schema s0001 is gone
    report = crawl(reader, store, config=CrawlConfig(), now=lambda: NOW)
    assert report.withdrawn == 10
    assert all(t.ref.schema == "s0000" for t in store.iter_tables())


def test_failed_schema_is_not_withdrawn(store: SQLiteStore) -> None:
    reader = FakeReader(20, tables_per_schema=10)
    crawl(reader, store, config=CrawlConfig(), now=lambda: NOW)
    reader.fail_schemas = {"s0001"}
    report = crawl(reader, store, config=CrawlConfig(), now=lambda: NOW)
    assert report.withdrawn == 0 and len(report.errors) == 1
    assert len(live(store)) == 20


def test_incremental_uses_changed_since(store: SQLiteStore) -> None:
    reader = FakeReader(30, tables_per_schema=30, incremental=True)
    t_first = NOW
    crawl(reader, store, config=CrawlConfig(), now=lambda: t_first)
    assert all(c["changed_since"] is None for c in reader.table_calls)

    reader.table_calls.clear()
    reader.column_batches.clear()
    cut = BASE + timedelta(seconds=27)  # tables 28, 29 changed after the cut
    reader.altered = lambda n: BASE + timedelta(seconds=n)
    last = dict(store.crawl_state_get("crawl_last") or {})
    last["started_at"] = "2026-01-01T00:00:27.000000Z"
    store.crawl_state_set("crawl_last", last)
    reader.dropped = {5}
    report = crawl(reader, store, config=CrawlConfig(), now=lambda: NOW + timedelta(days=1))
    calls = reader.table_calls
    assert calls[0]["changed_since"] == cut  # changed-only listing first
    assert calls[1]["changed_since"] is None  # then the name-only listing
    assert report.tables_written == 2 and sum(reader.column_batches) == 2
    assert report.tables_seen == 29 and report.withdrawn == 1
    assert store.table(ObjectRef(None, "s0000", "t0000005")).withdrawn_at is not None  # type: ignore[union-attr]

    reader.table_calls.clear()
    crawl(reader, store, config=CrawlConfig(), incremental=False, now=lambda: NOW)
    assert all(c["changed_since"] is None for c in reader.table_calls)


def test_scope_filter_applied_and_excluded_never_stored(store: SQLiteStore) -> None:
    reader = FakeReader(10, tables_per_schema=10)
    cfg = CrawlConfig(exclude=("s0000.t000000[0-3]",))
    report = crawl(reader, store, config=cfg, now=lambda: NOW)
    assert report.tables_seen == 6 and len(live(store)) == 6
    assert store.table(ObjectRef(None, "s0000", "t0000002")) is None
    skipped = FakeReader(10, tables_per_schema=10)
    crawl(skipped, store, config=CrawlConfig(exclude=("s0000.*",)), now=lambda: NOW)
    assert skipped.table_calls == []  # a schema-wide exclude never lists the schema


def test_queries_counted_and_ledgered(store: SQLiteStore) -> None:
    reader = FakeReader(10, tables_per_schema=10)
    report = crawl(reader, store, config=CrawlConfig(), now=lambda: NOW)
    # list_catalogs + list_schemas + 1 table page + columns + constraints
    assert report.queries == 5
    assert report.bytes_billed is None
    assert store.ledger_total("crawl", "queries", None) == 5


class _NullStore:
    """Counts instead of storing: isolates the crawl's own memory from SQLite's."""

    def __init__(self) -> None:
        self.state: dict[str, Mapping[str, object]] = {}
        self.tables = 0

    def upsert_schemas(self, items: object) -> None: ...
    def upsert_tables(self, items: Sequence[Table]) -> None:
        self.tables += len(items)

    def upsert_columns(self, table: ObjectRef, cols: Sequence[Column]) -> None: ...
    def upsert_constraints(self, table: ObjectRef, cons: Sequence[Constraint]) -> None: ...
    def mark_withdrawn(self, refs: object, at: datetime) -> None: ...
    def iter_tables(self, *, include_withdrawn: bool = False) -> Iterator[Table]:
        return iter(())

    def crawl_state_get(self, key: str) -> Mapping[str, object] | None:
        return self.state.get(key)

    def crawl_state_set(self, key: str, value: Mapping[str, object]) -> None:
        self.state[key] = dict(value)

    def ledger_add(self, kind: str, amount: int, unit: str, note: str) -> None: ...

    @contextmanager
    def transaction(self) -> Iterator[None]:
        yield


def test_crawl_memory_bounded() -> None:
    reader = FakeReader(200_000, tables_per_schema=10_000)
    ns = _NullStore()
    tracemalloc.start()
    try:
        report = crawl(reader, ns, config=CrawlConfig(workers=2), now=lambda: NOW)  # type: ignore[arg-type]
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert report.tables_written == 200_000 and ns.tables == 200_000
    assert peak < 64 * 1024 * 1024, f"peak {peak / 2**20:.1f} MiB"


def test_incremental_restores_unchanged_table_missing_or_withdrawn(store: SQLiteStore) -> None:
    reader = FakeReader(10, tables_per_schema=10, incremental=True)
    crawl(reader, store, config=CrawlConfig(), now=lambda: NOW)
    gone = ObjectRef(None, "s0000", "t0000004")
    store.mark_withdrawn([gone], NOW)  # e.g. dropped by an older crawl, restored with old metadata
    assert store.table(gone).withdrawn_at is not None  # type: ignore[union-attr]
    reader.table_calls.clear()
    report = crawl(reader, store, config=CrawlConfig(), now=lambda: NOW + timedelta(days=1))
    assert reader.table_calls[0]["changed_since"] is not None
    assert report.tables_written == 1  # nothing changed, only the withdrawn table is re-read
    assert store.table(gone).withdrawn_at is None  # type: ignore[union-attr]
    assert len(store.columns(gone)) == 2


def test_scope_widened_reads_new_tables_even_when_unchanged(store: SQLiteStore) -> None:
    reader = FakeReader(10, tables_per_schema=10, incremental=True)
    narrow = CrawlConfig(exclude=("s0000.t000000[5-9]",))
    crawl(reader, store, config=narrow, now=lambda: NOW)
    assert len(live(store)) == 5
    report = crawl(reader, store, config=CrawlConfig(), now=lambda: NOW + timedelta(days=1))
    assert all(c["changed_since"] is None for c in reader.table_calls[-2:])  # baseline dropped
    assert len(live(store)) == 10 and report.tables_written == 10


def test_scope_change_drops_changed_since(store: SQLiteStore) -> None:
    reader = FakeReader(10, tables_per_schema=10, incremental=True)
    crawl(reader, store, config=CrawlConfig(), now=lambda: NOW)
    reader.table_calls.clear()
    crawl(reader, store, config=CrawlConfig(exclude=("x.*",)), now=lambda: NOW)
    assert all(c["changed_since"] is None for c in reader.table_calls)
    reader.table_calls.clear()
    crawl(reader, store, config=CrawlConfig(exclude=("x.*",)), now=lambda: NOW)
    assert reader.table_calls[0]["changed_since"] is not None  # same scope -> incremental again


def test_schema_reappears_is_restored(store: SQLiteStore) -> None:
    reader = FakeReader(20, tables_per_schema=10, incremental=True)
    crawl(reader, store, config=CrawlConfig(), now=lambda: NOW)
    reader.n_tables = 10
    assert crawl(reader, store, config=CrawlConfig(), now=lambda: NOW).withdrawn == 10
    reader.n_tables = 20
    report = crawl(reader, store, config=CrawlConfig(), now=lambda: NOW)
    assert report.tables_written == 10 and len(live(store)) == 20


def test_timeout_reaches_reader_from_config(store: SQLiteStore) -> None:
    from schemaguru.catalog import DBAPIExecutor
    from schemaguru.catalog.readers.duckdb import DuckDBReader

    class Recorder:
        def __init__(self) -> None:
            self.timeouts: list[float | None] = []
            self.inner = DBAPIExecutor(__import__("duckdb").connect())

        def run(self, sql: str, params: Sequence[object] = (), *, timeout_s: float | None = None):  # type: ignore[no-untyped-def]
            self.timeouts.append(timeout_s)
            return self.inner.run(sql, params, timeout_s=timeout_s)

    ex = Recorder()
    ex.inner._conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY)")
    reader = DuckDBReader(ex)  # type: ignore[arg-type]
    assert reader.timeout_s is None
    crawl(reader, store, config=CrawlConfig(timeout_s=7.5), now=lambda: NOW)
    assert ex.timeouts and all(t == 7.5 for t in ex.timeouts)


def test_many_schemas_resume_and_bounded_checkpoint(store: SQLiteStore) -> None:
    reader = FakeReader(6000, tables_per_schema=2, page_size=10)  # 3,000 schemas
    reader.interrupt_at_call = 1501
    with pytest.raises(KeyboardInterrupt):
        crawl(reader, store, config=CrawlConfig(), now=lambda: NOW)
    state = store.crawl_state_get("crawl")
    assert state is not None and len(str(state)) < 400  # one cursor, not a growing list
    assert len(live(store)) == 3000
    report = crawl(reader, store, config=CrawlConfig(), now=lambda: NOW)
    assert len(live(store)) == 6000 and report.withdrawn == 0 and not report.errors
    assert report.tables_written == 3000


def test_resume_whose_cursor_schema_vanished_recrawls(store: SQLiteStore) -> None:
    reader = FakeReader(30, tables_per_schema=10, page_size=5)
    reader.interrupt_at_call = 4  # second page of s0001
    with pytest.raises(KeyboardInterrupt):
        crawl(reader, store, config=CrawlConfig(page_size=5), now=lambda: NOW)
    reader.fail_schemas = set()
    reader.n_tables = 10  # s0001 (the cursor) and s0002 are gone
    report = crawl(reader, store, config=CrawlConfig(page_size=5), now=lambda: NOW)
    assert report.tables_written == 10 and not report.errors
    assert {t.ref.schema for t in store.iter_tables()} == {"s0000"}
