from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from schemaguru.config import Budget
from schemaguru.measure.backend import CostEstimate, MeasureResult
from schemaguru.measure.budget import BudgetGuard
from schemaguru.measure.requests import RowCount
from schemaguru.model.evidence import Evidence
from schemaguru.model.refs import ObjectRef
from schemaguru.store.sqlite import SQLiteStore

REQ = RowCount(ObjectRef(None, "shop", "orders"))
REQ2 = RowCount(ObjectRef(None, "shop", "customers"))


@pytest.fixture
def store(tmp_path: Path) -> SQLiteStore:
    return SQLiteStore(tmp_path / "s.db")


def _est(b: int | None, req: RowCount = REQ) -> CostEstimate:
    return CostEstimate(req, b, "unknown" if b is None else "dry_run")


def _res(b: int | None, req: RowCount = REQ) -> MeasureResult:
    return MeasureResult(req, {"row_count": 1}, Evidence(bytes_processed=b), None)


def test_admit_run_over_probe_over_total(store: SQLiteStore) -> None:
    g = BudgetGuard(Budget(max_bytes_per_probe=100, max_bytes_total=150), store)
    assert g.admit(_est(101)) == "over_probe"
    assert g.admit(_est(100)) == "run"
    g.record(_res(100))
    assert g.remaining_bytes() == 50
    assert g.admit(_est(60, REQ2)) == "over_total"
    assert g.admit(_est(50, REQ2)) == "run"


def test_unknown_cost_runs_until_total_spent(store: SQLiteStore) -> None:
    g = BudgetGuard(Budget(max_bytes_per_probe=100, max_bytes_total=150), store)
    assert g.admit(_est(None)) == "run"
    g.record(_res(150))
    assert g.remaining_bytes() == 0
    assert g.admit(_est(None, REQ2)) == "over_total"


def test_record_writes_ledger_and_falls_back_to_estimate(store: SQLiteStore) -> None:
    g = BudgetGuard(Budget(), store)
    assert g.admit(_est(40)) == "run"
    g.record(_res(None))  # the warehouse did not report bytes: the admitted estimate counts
    assert store.ledger_total("measure", "bytes", None) == 40
    assert store.ledger_total("measure", "queries", None) == 1
    assert g.spent_bytes == 40


def test_unlimited_budget(store: SQLiteStore) -> None:
    g = BudgetGuard(Budget(max_bytes_per_probe=None, max_bytes_total=None), store)
    assert g.admit(_est(10**15)) == "run"
    assert g.remaining_bytes() is None


def test_max_probes_and_seconds(store: SQLiteStore) -> None:
    g = BudgetGuard(Budget(max_probes=1), store)
    assert g.admit(_est(1)) == "run"
    assert g.admit(_est(1, REQ2)) == "over_total"
    clock = iter([0.0, 5.0])
    t = BudgetGuard(Budget(max_seconds=1.0), store, clock=lambda: next(clock))
    assert t.admit(_est(1)) == "over_total"


def _reqs(n: int) -> list[RowCount]:
    return [RowCount(ObjectRef(None, "shop", f"t{i}")) for i in range(n)]


def test_admitted_but_unrecorded_costs_count_toward_total(store: SQLiteStore) -> None:
    g = BudgetGuard(Budget(max_bytes_per_probe=None, max_bytes_total=10), store)
    verdicts = [g.admit(_est(10, r)) for r in _reqs(5)]
    assert verdicts == ["run", "over_total", "over_total", "over_total", "over_total"]
    assert g.remaining_bytes() == 0  # reserved bytes are not available
    g.record(_res(4, _reqs(1)[0]))  # actual spend replaces the reservation
    assert g.remaining_bytes() == 6
    assert g.admit(_est(6, _reqs(2)[1])) == "run"


def test_probe_larger_than_total_is_over_probe(store: SQLiteStore) -> None:
    g = BudgetGuard(Budget(max_bytes_per_probe=None, max_bytes_total=10), store)
    assert g.admit(_est(11)) == "over_probe"  # it could never fit: not run, never re-queued
    assert g.admit(_est(10)) == "run"


def test_unknown_spend_is_recorded_as_unknown_not_zero(store: SQLiteStore) -> None:
    g = BudgetGuard(Budget(max_bytes_per_probe=10, max_bytes_total=100), store)
    assert g.admit(_est(None)) == "run"
    assert g.remaining_bytes() == 90  # charged pessimistically at max_bytes_per_probe
    g.record(_res(None))
    assert g.unknown_spend == 1 and g.spent_bytes == 10
    assert store.ledger_total("measure", "queries", None) == 1
    assert store.ledger_total("measure.unknown_bytes", "queries", None) == 1
    assert store.ledger_total("measure.unknown_bytes", "bytes", None) == 10  # the charge
    assert store.ledger_total("measure", "bytes", None) == 0  # no fake "real" bytes


def test_new_run_keeps_bytes_resets_probes_and_clock(store: SQLiteStore) -> None:
    ticks = iter([0.0, 0.0, 100.0, 100.0, 100.0])
    g = BudgetGuard(Budget(max_probes=1, max_seconds=5.0, max_bytes_total=100), store,
                    clock=lambda: next(ticks))  # fmt: skip
    assert g.admit(_est(60)) == "run"
    g.record(_res(60))
    g.new_run()
    assert g.admit(_est(60, REQ2)) == "over_total"  # the bytes are shared across runs
    assert g.admit(_est(10, REQ2)) == "run"


def test_zero_total_allows_nothing(store: SQLiteStore) -> None:
    g = BudgetGuard(Budget(max_bytes_per_probe=None, max_bytes_total=0), store)
    assert g.admit(_est(400)) == "over_total" and g.admit(_est(None)) == "over_total"


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _cycle(g: BudgetGuard, n: int) -> int:
    """One run of up to ``n`` unknown-cost probes; returns how many were admitted and run."""
    g.new_run()
    ran = 0
    for r in _reqs(n):
        if g.admit(_est(None, r)) != "run":
            break
        g.record(_res(None, r))
        ran += 1
    return ran


def test_unknown_cost_probes_are_bounded_across_runs(store: SQLiteStore) -> None:
    clock = Clock()
    g = BudgetGuard(Budget(max_bytes_per_probe=10, max_bytes_total=100), store, clock=clock)
    total = sum(_cycle(g, 1000) for _ in range(5))  # 5 shared runs, one period
    assert total == 10  # 100 / 10 per probe, not 5000


def test_unknown_charge_falls_back_to_total(store: SQLiteStore) -> None:
    g = BudgetGuard(Budget(max_bytes_per_probe=None, max_bytes_total=100), store)
    assert _cycle(g, 50) == 1
    big = BudgetGuard(Budget(max_bytes_per_probe=1000, max_bytes_total=100), store)
    assert _cycle(big, 50) == 1  # a charge never exceeds the whole total


def test_budget_refills_after_period(store: SQLiteStore) -> None:
    clock = Clock()
    g = BudgetGuard(Budget(max_bytes_per_probe=10, max_bytes_total=100, period_s=60.0), store,
                    clock=clock)  # fmt: skip
    assert _cycle(g, 50) == 10 and _cycle(g, 50) == 0
    clock.t = 59.0
    assert _cycle(g, 50) == 0
    clock.t = 60.0
    assert g.remaining_bytes() == 100
    assert _cycle(g, 50) == 10
    assert g.lifetime_spent_bytes == 200


def test_no_refill_without_period(store: SQLiteStore) -> None:
    clock = Clock()
    g = BudgetGuard(Budget(max_bytes_per_probe=10, max_bytes_total=100, period_s=None), store,
                    clock=clock)  # fmt: skip
    assert _cycle(g, 50) == 10
    clock.t = 10.0**9
    assert _cycle(g, 50) == 0 and g.remaining_bytes() == 0


# --- final fix wave: one reservation per admission; a per-run deadline ----------------------


def test_identical_requests_in_flight_keep_both_reservations(store: SQLiteStore) -> None:
    """Two admissions under one key (two identical claim checks in flight) are two charges:
    the second must not overwrite the first's reservation."""
    g = BudgetGuard(Budget(max_bytes_per_probe=None, max_bytes_total=150), store)
    assert g.admit(_est(60)) == "run"
    assert g.admit(_est(60)) == "run"  # same request, still in flight
    assert g.remaining_bytes() == 30  # both charges count (the bug left 90)
    assert g.admit(_est(60)) == "over_total"
    g.record(_res(None))  # first finishes with no reported bytes: its estimate is spent
    assert g.remaining_bytes() == 30
    g.record(_res(None))
    assert g.remaining_bytes() == 30 and g.spent_bytes == 120
    g.record(_res(None))  # a third record without an admission charges nothing (control)
    assert g.spent_bytes == 120


def test_deadline_starts_when_entered_and_only_overrides_seconds(store: SQLiteStore) -> None:
    now = [0.0]
    budget = Budget(max_bytes_per_probe=None, max_bytes_total=100)
    g = BudgetGuard(budget, store, clock=lambda: now[0])
    with g.deadline(5.0):
        now[0] = 4.0
        g.new_run()  # a run that started late (waited for a lock) keeps the earlier deadline
        assert g.admit(_est(10)) == "run"
        now[0] = 6.0
        assert g.admit(_est(10, REQ2)) == "over_total"
    assert g.admit(_est(10, REQ2)) == "run"  # outside the block: no deadline (control)
    assert g.remaining_bytes() == 80  # the byte total is the shared one


# --- 0.2.0 hardening (A3.9, A3.10) --------------------------------------------------------------


def test_refill_with_inflight_never_overshoots(store: SQLiteStore) -> None:
    """A probe admitted in one period and recorded in the next is spent in the next: the
    refill leaves room for it, so a period never records more than ``max_bytes_total``."""
    clock = Clock()
    g = BudgetGuard(Budget(max_bytes_per_probe=None, max_bytes_total=1000, period_s=60.0), store,
                    clock=clock)  # fmt: skip
    slow = RowCount(ObjectRef(None, "shop", "slow"))
    assert g.admit(_est(400, slow)) == "run"  # still running when the period turns over
    clock.t = 61.0
    assert g.remaining_bytes() == 600  # the in-flight charge is taken off the fresh total
    admitted = [r for r in _reqs(3) if g.admit(_est(400, r)) == "run"]
    assert len(admitted) == 1
    g.record(_res(400, slow))
    for r in admitted:
        g.record(_res(400, r))
    assert g.spent_bytes <= 1000 and g.spent_bytes == 800
    # negative control: with nothing in flight the whole total is admitted again
    clock.t = 122.0
    assert g.remaining_bytes() == 1000


def test_fused_probe_charged_once(shop_store: SQLiteStore, shop_duckdb: Any) -> None:
    """FactLang 0.1.4 fuses plans on one table into one query and gives every member's
    ``Measurement.evidence`` the whole query's ``bytes_processed`` (plus ``fused_with``). The
    budget must charge that query once, not once per request it served."""
    import dataclasses

    from schemaguru.measure import flbridge
    from schemaguru.measure.catalog_bridge import StoreCatalog
    from schemaguru.measure.requests import ProfileColumns, Uniqueness

    if "measure" not in flbridge.capabilities():
        pytest.skip("factlang capability measure not yet available")
    import factlang.connectors as flc

    class Billed:
        """FactLang's DuckDB connector, billing 1,000 bytes per query it runs."""

        def __init__(self, inner: Any) -> None:
            self.inner, self.runs = inner, 0
            self.dialect, self.paramstyle = inner.dialect, inner.paramstyle

        def execute(self, probe: Any, *, timeout_s: float | None) -> Any:
            self.runs += 1
            return dataclasses.replace(self.inner.execute(probe, timeout_s=timeout_s),
                                       bytes_processed=1000)  # fmt: skip

        def __getattr__(self, name: str) -> Any:
            return getattr(self.inner, name)

    conn = Billed(flc.DBAPIConnector(shop_duckdb[0], "duckdb"))
    backend = flbridge.FactLangBackend(conn, catalog=StoreCatalog(shop_store))
    orders = ObjectRef(None, "shop", "orders")
    reqs = [RowCount(orders), ProfileColumns(orders, ("status", "deleted")),
            Uniqueness(orders, ("order_id",))]  # fmt: skip
    g = BudgetGuard(Budget(max_bytes_per_probe=None, max_bytes_total=None), shop_store)
    for r in reqs:
        assert g.admit(CostEstimate(r, None, "unknown")) == "run"
    results = backend.measure(reqs)
    assert all(r.error is None for r in results)
    for res in results:
        g.record(res)
    plans = sum(len(flbridge.plans_for(r, flbridge._IR())) for r in reqs)
    assert conn.runs < plans  # FactLang did fuse
    assert g.spent_bytes == 1000 * conn.runs
    assert shop_store.ledger_total("measure", "bytes", None) == 1000 * conn.runs
