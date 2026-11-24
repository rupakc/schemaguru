"""Per-run measurement budget (spec 8.3): admit priced probes, record what they spent."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Literal

from schemaguru.config import Budget
from schemaguru.measure.backend import CostEstimate, MeasureResult
from schemaguru.measure.requests import Request
from schemaguru.store.protocol import Store

Admission = Literal["run", "over_probe", "over_total"]
LEDGER_KIND = "measure"
# one row per probe whose spend is unknown (no reported bytes, no estimate): never written as 0
LEDGER_UNKNOWN = "measure.unknown_bytes"


def _label(req: Request) -> str:
    return f"{type(req).__name__} {req.table_ref.canonical()}"


class BudgetGuard:
    """``over_probe``: this probe alone exceeds ``max_bytes_per_probe`` (not run, ERROR
    ``over_budget``; others continue) -- as is a probe costing more than the whole total, which
    could never fit. ``over_total``: the total bytes (spent plus admitted-but-unrecorded
    estimates), probe count or seconds are exhausted (the scheduler stops and reports the
    remaining queue). A probe of unknown cost is admitted while total bytes remain; what it
    spends is recorded, and an unknown spend is counted as unknown, never as 0 bytes.

    The byte total spans every run that shares the guard (:meth:`new_run` resets only the probe
    count and the clock), so a background drainer cannot spend ``max_bytes_total`` per loop."""

    def __init__(
        self, budget: Budget, store: Store, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._budget = budget
        self._store = store
        self._clock = clock
        self._start = clock()
        self._reserved: dict[Request, int | None] = {}
        self.spent_bytes = 0
        self.probes = 0
        self.unknown_cost = 0  # probes admitted without a known estimate
        self.unknown_spend = 0  # probes recorded with no reported bytes and no estimate

    def new_run(self) -> None:
        """Start another run on the same byte budget (probe count and clock restart)."""
        self._start = self._clock()
        self.probes = 0

    def _committed(self) -> int:
        return self.spent_bytes + sum(c for c in self._reserved.values() if c is not None)

    def admit(self, estimate: CostEstimate) -> Admission:
        b = self._budget
        if b.max_probes is not None and self.probes >= b.max_probes:
            return "over_total"
        if b.max_seconds is not None and self._clock() - self._start > b.max_seconds:
            return "over_total"
        cost = estimate.bytes
        if cost is not None and b.max_bytes_per_probe is not None and cost > b.max_bytes_per_probe:
            return "over_probe"
        committed = self._committed()
        if b.max_bytes_total is not None and committed >= b.max_bytes_total:
            return "over_total"  # nothing left (a zero total allows nothing)
        if cost is not None and b.max_bytes_total is not None and cost > b.max_bytes_total:
            return "over_probe"  # could never fit: not run, and not left blocking the queue
        if (
            b.max_bytes_total is not None
            and cost is not None
            and committed + cost > b.max_bytes_total
        ):
            return "over_total"
        self.probes += 1
        if cost is None:
            self.unknown_cost += 1
        self._reserved[estimate.request] = cost
        return "run"

    def record(self, result: MeasureResult) -> None:
        """Write what the probe spent to the ledger (reported bytes, else the admitted estimate)."""
        reported = None if result.evidence is None else result.evidence.bytes_processed
        reserved = self._reserved.pop(result.request, None)
        spent = reported if reported is not None else reserved
        note = _label(result.request)
        if spent is None:
            self.unknown_spend += 1
            self._store.ledger_add(LEDGER_UNKNOWN, 1, "queries", note)
        else:
            self.spent_bytes += int(spent)
            self._store.ledger_add(LEDGER_KIND, int(spent), "bytes", note)
        self._store.ledger_add(LEDGER_KIND, 1, "queries", note)

    def remaining_bytes(self) -> int | None:
        total = self._budget.max_bytes_total
        return None if total is None else max(0, total - self._committed())
