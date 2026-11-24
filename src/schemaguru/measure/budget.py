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


def _label(req: Request) -> str:
    return f"{type(req).__name__} {req.table_ref.canonical()}"


class BudgetGuard:
    """``over_probe``: this probe alone exceeds ``max_bytes_per_probe`` (not run, ERROR
    ``over_budget``; others continue). ``over_total``: the run's total bytes, probe count or
    seconds are exhausted (the scheduler stops and reports the remaining queue). A probe of
    unknown cost is admitted while total bytes remain; what it spends is recorded."""

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

    def admit(self, estimate: CostEstimate) -> Admission:
        b = self._budget
        if b.max_probes is not None and self.probes >= b.max_probes:
            return "over_total"
        if b.max_seconds is not None and self._clock() - self._start > b.max_seconds:
            return "over_total"
        cost = estimate.bytes
        if cost is not None and b.max_bytes_per_probe is not None and cost > b.max_bytes_per_probe:
            return "over_probe"
        if b.max_bytes_total is not None and (
            self.spent_bytes >= b.max_bytes_total
            or (cost is not None and self.spent_bytes + cost > b.max_bytes_total)
        ):
            return "over_total"
        self.probes += 1
        self._reserved[estimate.request] = cost
        return "run"

    def record(self, result: MeasureResult) -> None:
        """Write what the probe spent to the ledger (reported bytes, else the admitted estimate)."""
        reported = None if result.evidence is None else result.evidence.bytes_processed
        spent = reported if reported is not None else self._reserved.pop(result.request, None)
        self._reserved.pop(result.request, None)
        spent = int(spent or 0)
        self.spent_bytes += spent
        note = _label(result.request)
        self._store.ledger_add(LEDGER_KIND, spent, "bytes", note)
        self._store.ledger_add(LEDGER_KIND, 1, "queries", note)

    def remaining_bytes(self) -> int | None:
        total = self._budget.max_bytes_total
        return None if total is None else max(0, total - self.spent_bytes)
