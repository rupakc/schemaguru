from __future__ import annotations

from pathlib import Path

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
