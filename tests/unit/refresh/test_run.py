from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from schemaguru.catalog import DBAPIExecutor, crawl
from schemaguru.catalog.readers.duckdb import DuckDBReader
from schemaguru.config import Budget, Config
from schemaguru.knowledge.diff import subject_key
from schemaguru.measure.requests import KeyOverlapReq, Uniqueness
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
    assert {"NULL_RATE", "CATALOG"} <= r1.run_checks
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
    assert "NULL_RATE" not in r.run_checks


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
    assert {"JOIN", "PRIMARY_KEY", "NULL_RATE"} <= first.run_checks
    cfg = Config()
    reader = DuckDBReader(DBAPIExecutor(conn))
    narrow = NarrowScheduler(ObjectRef(None, "shop", "customers"))
    r = refresh(store, reader, narrow, config=cfg, now=later)  # type: ignore[arg-type]
    assert r.run_checks >= {"NULL_RATE", "CATALOG"} and "JOIN" not in r.run_checks
    assert {"JOIN", "PRIMARY_KEY"} <= set(r.missing_checks)
    by_class = {k for _, k in r.changes}
    assert by_class == {ChangeClass.NOT_MEASURED}  # never "removed", never "fixed"
    unmeasured = {(c.kind.name, subject_key(c.subject)) for c, _ in r.changes}
    tables = {(k, sub.split(".")[1]) for k, sub in unmeasured if k != "JOIN"}
    assert ("PRIMARY_KEY", "customers") in tables  # kind not run
    assert ("NULL_RATE", "orders") in tables  # kind ran, table never reached
    # a scheduler that claims a table but never re-stamped its facts is not believed
    assert ("NULL_RATE", "customers") in tables
    assert r.exit_code == 0
    d = json.loads(r.to_json())
    assert "JOIN" in d["missing_checks"] and "sampled_tables" in d["measure"]
    assert "run_checks" in d and "carried_checks" in d and "checks_run" not in d
    assert "missing checks" in r.to_markdown() and "Not measured" in r.to_markdown()


def test_vanished_fact_of_unrun_check_not_removed(store: SQLiteStore, conn: Any) -> None:
    """refresh diffs with the new snapshot's checks_run: a fact that vanished while its check
    did not run is not a removal; one whose check ran is."""
    go(store, conn)
    facts = store.facts_for(None, ["JOIN", "NULL_RATE"])
    join = next(f for f in facts if f.kind.name == "JOIN" and f.provenance.value == "MEASURED")
    nulls = next(
        f for f in facts if f.kind.name == "NULL_RATE" and "customers" in f.subject.canonical()
    )
    store.delete_facts([join.id, nulls.id])
    reader = DuckDBReader(DBAPIExecutor(conn))
    narrow = NarrowScheduler(ObjectRef(None, "shop", "customers"))
    r = refresh(store, reader, narrow, config=Config(), now=later)  # type: ignore[arg-type]
    removed = {
        c.before.id: k for c, k in r.changes if c.change == "removed" and c.before is not None
    }
    assert nulls.id in removed
    # the JOIN check did not run: its vanished fact is reported NOT_MEASURED, never "fixed"
    assert removed.get(join.id) is ChangeClass.NOT_MEASURED


def test_fresh_noop_is_not_a_narrow_run(store: SQLiteStore, conn: Any) -> None:
    first = go(store, conn)
    again = go(store, conn)
    assert again.missing_checks == [] and again.carried_checks >= first.run_checks - {"CATALOG"}


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
    assert "CATALOG" not in r.run_checks


def test_crawl_errors_with_nothing_seen_exit_3(store: SQLiteStore, conn: Any) -> None:
    from tests.unit.catalog.fake_reader import FakeReader

    cfg = Config()
    reader = FakeReader(2, tables_per_schema=1, fail_schemas=["s0000", "s0001"])
    r = refresh(store, reader, NarrowScheduler(ObjectRef(None, "x", "y")), config=cfg)  # type: ignore[arg-type]
    assert r.crawl.errors and r.crawl.tables_seen == 0
    assert r.exit_code == 3 and "CATALOG" not in r.run_checks


def test_partial_crawl_has_no_catalog_check(store: SQLiteStore, conn: Any) -> None:
    from tests.unit.catalog.fake_reader import FakeReader

    reader = FakeReader(2, tables_per_schema=1, fail_schemas=["s0001"])
    r = refresh(
        store, reader, NarrowScheduler(ObjectRef(None, "s0000", "t0000000")), config=Config()
    )  # type: ignore[arg-type]
    assert r.crawl.errors and r.crawl.tables_seen > 0
    assert "CATALOG" not in r.run_checks


def test_measure_errors_exit_3_and_check_not_run(store: SQLiteStore, conn: Any) -> None:
    r = go(store, conn, fail=lambda req: isinstance(req, Uniqueness))
    assert r.measure.errors and r.exit_code == 3
    assert "PRIMARY_KEY" not in r.run_checks and "NULL_RATE" in r.run_checks


class NoJoinScheduler(Scheduler):
    """A config that stopped measuring joins."""

    def plan_table(self, ref: ObjectRef) -> list[Any]:
        return [r for r in super().plan_table(ref) if not isinstance(r, KeyOverlapReq)]


def _clocked(
    store: SQLiteStore,
    conn: Any,
    clock: list[datetime],
    cls: type = Scheduler,
    cfg: Config | None = None,
    **kw: Any,
) -> Any:
    def now() -> datetime:
        return clock[0]

    cfg = cfg or Config()
    reader = DuckDBReader(DBAPIExecutor(conn))
    return refresh(
        store, reader, cls(store, FakeBackend(conn, **kw), cfg, now=now), config=cfg, now=now
    )


def test_partial_run_no_false_alarm_and_later_narrowing_still_seen(
    store: SQLiteStore, conn: Any
) -> None:
    t0 = datetime.now(timezone.utc)
    clock = [t0]
    first = _clocked(store, conn, clock)
    assert "JOIN" in first.run_checks
    # run 2: only the new table is due, and it has no join: nothing is missing
    clock[0] = t0 + timedelta(days=1)
    conn.execute("CREATE TABLE shop.misc(id INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO shop.misc VALUES (1)")
    second = _clocked(store, conn, clock)
    assert [t.table for t in second.measure.measured_tables] == ["misc"]
    assert second.missing_checks == []
    assert not [c for c, k in second.changes if k is ChangeClass.NOT_MEASURED]
    assert "JOIN" in second.carried_checks  # still stands on the tables that were not due
    recorded = store.snapshot_checks_run(second.snapshot_id or "")
    assert "carried:JOIN" in recorded and "JOIN" not in recorded  # carried is never "run"
    # run 3: every table is due and the config no longer measures joins
    clock[0] = t0 + timedelta(days=40)
    third = _clocked(store, conn, clock, NoJoinScheduler)
    assert "JOIN" in third.missing_checks and "JOIN" not in third.run_checks
    joins = [(c, k) for c, k in third.changes if c.kind.name == "JOIN"]
    assert joins and {k for _, k in joins} == {ChangeClass.NOT_MEASURED}
    assert not [1 for _, k in third.changes if k is ChangeClass.REVIEW]
    assert third.exit_code == 0


def test_restore_and_redrop_between_direct_crawls_is_reported(
    store: SQLiteStore, conn: Any
) -> None:
    go(store, conn)
    conn.execute("DROP TABLE shop.orders")
    r2 = go(store, conn)
    assert [c for c, _ in r2.changes if c.kind.name == "TABLE_WITHDRAWN"]
    reader = DuckDBReader(DBAPIExecutor(conn))
    conn.execute("CREATE TABLE shop.orders(order_id INTEGER PRIMARY KEY, customer_id INTEGER)")
    crawl(reader, store, now=lambda: later(1))
    conn.execute("DROP TABLE shop.orders")
    crawl(reader, store, now=lambda: later(2))
    r3 = go(store, conn)
    assert [c for c, _ in r3.changes if c.kind.name == "TABLE_WITHDRAWN"]
    r4 = go(store, conn)
    assert not [c for c, _ in r4.changes if c.kind.name == "TABLE_WITHDRAWN"]


def test_failing_classifier_does_not_mark_withdrawal_reported(
    store: SQLiteStore, conn: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import schemaguru.refresh.run as run_mod

    go(store, conn)
    conn.execute("DROP TABLE shop.orders")

    def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("classifier bug")

    with monkeypatch.context() as m:
        m.setattr(run_mod, "classify_changes", boom)
        with pytest.raises(RuntimeError):
            go(store, conn)
    r = go(store, conn)
    assert [c for c, _ in r.changes if c.kind.name == "TABLE_WITHDRAWN"]


def test_due_table_does_not_vouch_for_kinds() -> None:
    """Fresh facts on a due table (altered since) do not stand in for a kind the run skipped."""
    from schemaguru.model.facts import Fact, Provenance, Status
    from schemaguru.model.kinds import FactKind
    from schemaguru.refresh.run import _carried_kinds

    t = datetime(2026, 11, 24, tzinfo=timezone.utc)

    def nr(table: str) -> Fact:
        return Fact.make(
            FactKind.NULL_RATE, ObjectRef(None, "shop", table, "c"), {"rate": 0.0},
            Provenance.MEASURED, Status.OBSERVED, method="exact", observed_at=t,
        )  # fmt: skip

    facts = [nr("a"), nr("b")]
    due = [ObjectRef(None, "shop", "a")]
    assert _carried_kinds(facts, due, t, 14) == {"NULL_RATE"}  # b is not due and fresh
    assert _carried_kinds(facts[:1], due, t, 14) == set()  # only the due table held it
    assert _carried_kinds(facts, [], t + timedelta(days=30), 14) == set()  # stale: not standing


def test_errored_check_old_facts_are_not_measured_never_fixed(
    store: SQLiteStore, conn: Any
) -> None:
    from schemaguru.measure.requests import ProfileColumns

    t0 = datetime.now(timezone.utc)
    clock = [t0]
    _clocked(store, conn, clock)
    clock[0] = t0 + timedelta(days=30)
    r = _clocked(store, conn, clock, fail=lambda req: isinstance(req, ProfileColumns))
    assert "NULL_RATE" not in r.run_checks and "NULL_RATE" not in r.carried_checks
    got = {k for c, k in r.changes if c.kind.name == "NULL_RATE"}
    assert ChangeClass.NOT_MEASURED in got and ChangeClass.REVIEW not in got


def test_advanced_fact_is_not_carried() -> None:
    from schemaguru.model.facts import Fact, Provenance, Status
    from schemaguru.model.kinds import FactKind
    from schemaguru.refresh.run import _carried_unmeasured

    t = datetime(2026, 11, 24, tzinfo=timezone.utc)
    ref = ObjectRef(None, "shop", "orders", "c")

    def nr(at: datetime) -> Fact:
        return Fact.make(
            FactKind.NULL_RATE, ref, {"rate": 0.0},
            Provenance.MEASURED, Status.OBSERVED, method="exact", observed_at=at,
        )  # fmt: skip

    due = [ref.table_ref()]
    same = _carried_unmeasured([nr(t)], [nr(t)], due, True)
    advanced = _carried_unmeasured([nr(t)], [nr(t + timedelta(days=1))], due, True)
    assert len(same) == 1 and advanced == []


class OrdersNoJoinScheduler(Scheduler):
    """JOIN still runs for other tables, but its request is dropped on orders."""

    def plan_table(self, ref: ObjectRef) -> list[Any]:
        reqs = super().plan_table(ref)
        if ref.table == "orders":
            reqs = [r for r in reqs if not isinstance(r, KeyOverlapReq)]
        return reqs


def test_kind_ran_elsewhere_does_not_vouch_for_a_dropped_probe(
    store: SQLiteStore, conn: Any
) -> None:
    conn.execute("CREATE TABLE shop.lines(line_id INTEGER PRIMARY KEY, customer_id INTEGER)")
    conn.execute("INSERT INTO shop.lines VALUES (1, 1)")
    t0 = datetime.now(timezone.utc)
    clock = [t0]
    first = _clocked(store, conn, clock)
    joins = {subject_key(c.subject) for c, _ in first.changes if c.kind.name == "JOIN"}
    assert any("orders" in j for j in joins) and any("lines" in j for j in joins)
    clock[0] = t0 + timedelta(days=40)
    r = _clocked(store, conn, clock, OrdersNoJoinScheduler)
    assert "JOIN" in r.run_checks  # it did run, on lines
    flagged = [(subject_key(c.subject), k) for c, k in r.changes if c.kind.name == "JOIN"]
    assert flagged and all(k is ChangeClass.NOT_MEASURED for _, k in flagged)
    assert all("orders" in j and "lines" not in j for j, _ in flagged)
    assert "JOIN" in r.missing_checks and r.exit_code == 0


def _not_measured(r: Any) -> list[str]:
    return [
        f"{c.kind.name} {subject_key(c.subject)}"
        for c, k in r.changes
        if k is ChangeClass.NOT_MEASURED and c.change == "removed"
    ]


def test_error_then_recovery_reports_nothing_missing(store: SQLiteStore, conn: Any) -> None:
    t0 = datetime.now(timezone.utc)
    clock = [t0]
    _clocked(store, conn, clock)
    clock[0] = t0 + timedelta(days=30)
    bad = _clocked(store, conn, clock, fail=lambda req: isinstance(req, Uniqueness))
    assert bad.measure.errors
    clock[0] = t0 + timedelta(days=60)
    ok = _clocked(store, conn, clock)  # the probe recovered
    assert ok.missing_checks == [] and _not_measured(ok) == []
    clock[0] = t0 + timedelta(days=90)
    again = _clocked(store, conn, clock)
    assert again.missing_checks == [] and _not_measured(again) == []


def test_exact_then_sampled_does_not_flag_the_exact_fact(store: SQLiteStore, conn: Any) -> None:
    from schemaguru.config import SamplingConfig

    t0 = datetime.now(timezone.utc)
    clock = [t0]
    _clocked(store, conn, clock)
    clock[0] = t0 + timedelta(days=30)
    forced = Config(sampling=SamplingConfig(measurement="force"))
    r = _clocked(store, conn, clock, cfg=forced)
    sampled = [f for f in store.iter_facts() if (f.method or "").startswith("sampled:")]
    assert sampled  # the table really switched to sampling
    # every re-measured fact superseded its exact twin; only enum discovery (which a sampled
    # run does not produce for these tables) is genuinely absent
    assert set(r.missing_checks) <= {"VALUES"}
    assert all(x.startswith("VALUES") for x in _not_measured(r))


def test_withdrawn_table_is_not_also_not_measured(store: SQLiteStore, conn: Any) -> None:
    t0 = datetime.now(timezone.utc)
    clock = [t0]
    _clocked(store, conn, clock)
    conn.execute("DROP TABLE shop.orders")
    clock[0] = t0 + timedelta(days=30)  # customers is due again; orders is gone
    r = _clocked(store, conn, clock)
    assert [c for c, _ in r.changes if c.kind.name == "TABLE_WITHDRAWN"]
    assert [x for x in _not_measured(r) if "orders" in x] == []


def test_carried_error_fact_is_superseded_not_missing() -> None:
    from schemaguru.model.facts import Fact, Provenance, Status
    from schemaguru.model.kinds import FactKind
    from schemaguru.refresh.run import _carried_unmeasured

    t = datetime(2026, 11, 24, tzinfo=timezone.utc)
    ref = ObjectRef(None, "shop", "orders", "c")

    def nr(st: Status) -> Fact:
        return Fact.make(
            FactKind.NULL_RATE, ref, {"rate": None if st is Status.ERROR else 0.0},
            Provenance.MEASURED, st, method="exact", observed_at=t,
        )  # fmt: skip

    due = [ref.table_ref()]
    err = nr(Status.ERROR)
    assert _carried_unmeasured([err], [err], due, True) == []
    ok = nr(Status.OBSERVED)
    assert len(_carried_unmeasured([ok], [ok], due, True)) == 1
