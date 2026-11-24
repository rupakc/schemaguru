from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from schemaguru.catalog import DBAPIExecutor
from schemaguru.catalog.readers.duckdb import DuckDBReader
from schemaguru.config import Budget, Config
from schemaguru.measure.scheduler import Scheduler
from schemaguru.refresh import ChangeClass, refresh
from schemaguru.store.sqlite import SQLiteStore
from tests.unit.measure.fake_backend import FakeBackend

duckdb = pytest.importorskip("duckdb")


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
    clean = go(store, conn)
    again = go(store, conn)
    assert again.exit_code == 0
    assert clean.exit_code in (0, 1)
    conn.execute("DROP TABLE shop.orders")
    assert go(store, conn).exit_code == 1


def test_nothing_measured_exit_3(store: SQLiteStore, conn: Any) -> None:
    cfg = Config(budget=Budget(max_bytes_total=0, max_bytes_per_probe=None))
    r = go(store, conn, cfg, cost=lambda req: 400)
    assert r.measure.measured_tables == [] and r.measure.over_budget
    assert r.exit_code == 3
    assert "NULL_RATE" not in r.checks_run


def test_narrow_run_reports_not_measured(store: SQLiteStore, conn: Any) -> None:
    from schemaguru.knowledge.diff import diff
    from schemaguru.refresh.classify import classify_changes

    first = go(store, conn)
    prev = store.snapshot_facts(first.snapshot_id or "")
    changes = diff(prev, [])
    out = classify_changes(changes, {}, {}, checks_run={"CATALOG"}, measured_tables=[])
    kinds = {k for _, k in out}
    assert ChangeClass.NOT_MEASURED in kinds
    assert not any(
        k is ChangeClass.REVIEW and c.before is not None and c.before.provenance.value == "MEASURED"
        for c, k in out
    )
