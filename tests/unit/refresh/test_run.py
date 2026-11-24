from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from schemaguru.catalog import DBAPIExecutor
from schemaguru.catalog.readers.duckdb import DuckDBReader
from schemaguru.config import Budget, Config
from schemaguru.knowledge.diff import subject_key
from schemaguru.measure.requests import Uniqueness
from schemaguru.measure.scheduler import MeasureReport, Scheduler
from schemaguru.model.refs import ObjectRef
from schemaguru.refresh import ChangeClass, refresh
from schemaguru.store.sqlite import SQLiteStore
from tests.unit.measure.fake_backend import FakeBackend

duckdb = pytest.importorskip("duckdb")
FIRST_RUN_EXIT = 0


def go(store: SQLiteStore, conn: Any, cfg: Config | None = None, **kw: Any) -> Any:
    cfg = cfg or Config()
    reader = DuckDBReader(DBAPIExecutor(conn))
    return refresh(store, reader, Scheduler(store, FakeBackend(conn, **kw), cfg), config=cfg)


def test_first_then_second_refresh(store: SQLiteStore, conn: Any) -> None:
    r1 = go(store, conn)
    assert r1.measure.measured_tables and r1.exit_code != 3
    assert {"NULL_RATE", "CATALOG"} <= r1.checks_run
    assert r1.previous_snapshot_id is None and r1.snapshot_id
    r2 = go(store, conn)  # nothing stale or changed: nothing to measure, clean
    assert r2.previous_snapshot_id == r1.snapshot_id
    assert r2.changes == [] and r2.exit_code == 0


def test_stale_facts_are_remeasured(store: SQLiteStore, conn: Any) -> None:
    go(store, conn)
    later = datetime.now(timezone.utc) + timedelta(days=30)
    cfg = Config()
    reader = DuckDBReader(DBAPIExecutor(conn))
    sched = Scheduler(store, FakeBackend(conn), cfg, now=lambda: later)
    r = refresh(store, reader, sched, config=cfg, now=lambda: later)
    assert len(r.measure.measured_tables) == 2


def test_withdrawn_table_review(store: SQLiteStore, conn: Any) -> None:
    go(store, conn)
    conn.execute("DROP TABLE shop.orders")
    r = go(store, conn)
    withdrawn = [(c, k) for c, k in r.changes if c.kind.name == "TABLE_WITHDRAWN"]
    assert [k for _, k in withdrawn] == [ChangeClass.REVIEW]
    assert withdrawn[0][0].subject.canonical().endswith("orders")  # type: ignore[union-attr]
    assert r.exit_code == 1


def test_exit_codes(store: SQLiteStore, conn: Any) -> None:
    assert go(store, conn).exit_code == FIRST_RUN_EXIT
    assert go(store, conn).exit_code == 0
    conn.execute("DROP TABLE shop.orders")
    assert go(store, conn).exit_code == 1


def test_nothing_measured_exit_3(store: SQLiteStore, conn: Any) -> None:
    cfg = Config(budget=Budget(max_bytes_total=0, max_bytes_per_probe=None))
    r = go(store, conn, cfg, cost=lambda req: 400)
    assert r.measure.measured_tables == [] and r.measure.over_budget
    assert r.exit_code == 3
    assert "NULL_RATE" not in r.checks_run


class NarrowScheduler:
    """Measures one table and one check only, writes nothing."""

    def __init__(self, table: ObjectRef) -> None:
        self.table = table

    def run(self, refs: Any = None, **kw: Any) -> MeasureReport:
        return MeasureReport(measured_tables=[self.table], checks_run={"NULL_RATE"})


def later(days: int = 30) -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=days)


def test_narrow_run_reports_not_measured(store: SQLiteStore, conn: Any) -> None:
    """End to end: refresh twice, the second run measures less than the first."""
    first = go(store, conn)
    assert {"JOIN", "PRIMARY_KEY", "NULL_RATE"} <= first.checks_run
    cfg = Config()
    reader = DuckDBReader(DBAPIExecutor(conn))
    narrow = NarrowScheduler(ObjectRef(None, "shop", "customers"))
    r = refresh(store, reader, narrow, config=cfg, now=later)  # type: ignore[arg-type]
    assert r.checks_run >= {"NULL_RATE", "CATALOG"} and "JOIN" not in r.checks_run
    assert {"JOIN", "PRIMARY_KEY"} <= set(r.missing_checks)
    by_class = {k for _, k in r.changes}
    assert by_class == {ChangeClass.NOT_MEASURED}  # never "removed", never "fixed"
    unmeasured = {(c.kind.name, subject_key(c.subject)) for c, _ in r.changes}
    tables = {(k, sub.split(".")[1]) for k, sub in unmeasured if k != "JOIN"}
    assert ("PRIMARY_KEY", "customers") in tables  # kind not run
    assert ("NULL_RATE", "orders") in tables  # kind ran, table never reached
    assert ("NULL_RATE", "customers") not in tables  # measured: not unknown
    assert r.exit_code == 0
    d = json.loads(r.to_json())
    assert "JOIN" in d["missing_checks"] and "sampled_tables" in d["measure"]
    assert "missing checks" in r.to_markdown() and "Not measured" in r.to_markdown()


def test_fresh_noop_is_not_a_narrow_run(store: SQLiteStore, conn: Any) -> None:
    first = go(store, conn)
    again = go(store, conn)
    assert again.missing_checks == [] and again.checks_run >= first.checks_run - {"CATALOG"}


def test_withdrawal_survives_a_scheduler_failure(store: SQLiteStore, conn: Any) -> None:
    go(store, conn)
    conn.execute("DROP TABLE shop.orders")

    class Boom:
        def run(self, refs: Any = None, **kw: Any) -> MeasureReport:
            raise RuntimeError("backend down")

    reader = DuckDBReader(DBAPIExecutor(conn))
    r = refresh(store, reader, Boom(), config=Config(), now=later)  # type: ignore[arg-type]
    assert any(c.kind.name == "TABLE_WITHDRAWN" for c, _ in r.changes)
    assert r.exit_code == 3 and r.measure.errors  # recorded, not raised


def test_withdrawal_survives_a_crash_between_crawl_and_snapshot(
    store: SQLiteStore, conn: Any
) -> None:
    go(store, conn)
    conn.execute("DROP TABLE shop.orders")

    class Crash:
        def run(self, refs: Any = None, **kw: Any) -> MeasureReport:
            raise KeyboardInterrupt

    reader = DuckDBReader(DBAPIExecutor(conn))
    with pytest.raises(KeyboardInterrupt):
        refresh(store, reader, Crash(), config=Config(), now=later)  # type: ignore[arg-type]
    r = refresh(
        store,
        reader,
        NarrowScheduler(ObjectRef(None, "shop", "customers")),
        config=Config(),
        now=later,
    )  # type: ignore[arg-type]
    withdrawn = [c for c, _ in r.changes if c.kind.name == "TABLE_WITHDRAWN"]
    assert len(withdrawn) == 1  # the interrupted run did not swallow it
    again = go(store, conn)
    assert not [c for c, _ in again.changes if c.kind.name == "TABLE_WITHDRAWN"]  # reported once


class Boom:
    dialect = "boom"

    def capabilities(self) -> Any:
        raise RuntimeError("cannot connect")


def test_raising_reader_is_recorded_exit_3(store: SQLiteStore, conn: Any) -> None:
    cfg = Config()
    r = refresh(store, Boom(), Scheduler(store, FakeBackend(conn), cfg), config=cfg)  # type: ignore[arg-type]
    assert r.crawl.errors and r.crawl.tables_seen == 0 and r.exit_code == 3
    assert "CATALOG" not in r.checks_run


def test_crawl_errors_with_nothing_seen_exit_3(store: SQLiteStore, conn: Any) -> None:
    from tests.unit.catalog.fake_reader import FakeReader

    cfg = Config()
    reader = FakeReader(2, tables_per_schema=1, fail_schemas=["s0000", "s0001"])
    r = refresh(store, reader, NarrowScheduler(ObjectRef(None, "x", "y")), config=cfg)  # type: ignore[arg-type]
    assert r.crawl.errors and r.crawl.tables_seen == 0
    assert r.exit_code == 3 and "CATALOG" not in r.checks_run


def test_partial_crawl_has_no_catalog_check(store: SQLiteStore, conn: Any) -> None:
    from tests.unit.catalog.fake_reader import FakeReader

    reader = FakeReader(2, tables_per_schema=1, fail_schemas=["s0001"])
    r = refresh(
        store, reader, NarrowScheduler(ObjectRef(None, "s0000", "t0000000")), config=Config()
    )  # type: ignore[arg-type]
    assert r.crawl.errors and r.crawl.tables_seen > 0
    assert "CATALOG" not in r.checks_run


def test_measure_errors_exit_3_and_check_not_run(store: SQLiteStore, conn: Any) -> None:
    r = go(store, conn, fail=lambda req: isinstance(req, Uniqueness))
    assert r.measure.errors and r.exit_code == 3
    assert "PRIMARY_KEY" not in r.checks_run and "NULL_RATE" in r.checks_run
