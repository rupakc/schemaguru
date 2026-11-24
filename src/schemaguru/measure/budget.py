"""Per-run measurement budget (spec 8.3): admit priced probes, record what they spent."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Hashable
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
    charges), probe count or seconds are exhausted (the scheduler stops and reports the
    remaining queue).

    A probe of unknown cost is charged pessimistically: at ``max_bytes_per_probe``, else at
    ``max_bytes_total`` (never more than the total), for admission and for the committed total.
    If the warehouse then reports no bytes either, the charge stays as the spend and is ledgered
    under ``measure.unknown_bytes`` (count and charge), never as 0 real bytes.

    The byte total spans every run that shares the guard (:meth:`new_run` resets only the probe
    count and the clock) and refills once per ``Budget.period_s`` (``None``: never), so a
    background drainer spends at most ``max_bytes_total`` per period."""

    def __init__(
        self, budget: Budget, store: Store, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._budget = budget
        self._store = store
        # admit/record may be called from several threads (the drainer, concurrent MCP calls)
        self._mu = threading.RLock()
        self._clock = clock
        now = clock()
        self._start = now
        self._period_start = now
        # request (or any key) -> (charged bytes counted in the committed total, known estimate)
        self._reserved: dict[Hashable, tuple[int | None, int | None]] = {}
        self.spent_bytes = 0  # this period, including pessimistic charges
        self.lifetime_spent_bytes = 0
        self.probes = 0
        self.unknown_cost = 0  # probes admitted without a known estimate
        self.unknown_spend = 0  # probes recorded with no reported bytes and no estimate

    def new_run(self) -> None:
        """Start another run on the same byte budget (probe count and clock restart)."""
        with self._mu:
            self._start = self._clock()
            self.probes = 0

    def rebudget(self, budget: Budget) -> None:
        """Apply a new ``Budget`` keeping what this period already spent (a config switch
        must not refill the allowance)."""
        with self._mu:
            self._budget = budget

    def _refill(self, now: float) -> None:
        period = self._budget.period_s
        if period is not None and now - self._period_start >= period:
            self._period_start = now
            self.spent_bytes = 0
            # in-flight probes stay tracked for recording, but no longer count against the
            # fresh period's total
            self._reserved = {r: (None, est) for r, (_, est) in self._reserved.items()}

    def _charge_unknown(self) -> int | None:
        b = self._budget
        options = [x for x in (b.max_bytes_per_probe, b.max_bytes_total) if x is not None]
        return min(options) if options else None

    def _committed(self) -> int:
        return self.spent_bytes + sum(c for c, _ in self._reserved.values() if c is not None)

    def admit(self, estimate: CostEstimate) -> Admission:
        return self.admit_key(estimate.request, estimate.bytes)

    def admit_key(self, key: Hashable, known: int | None) -> Admission:
        """Admit one priced unit of spend under ``key`` (``known=None``: unknown cost).

        :meth:`admit` is this for a probe; hosts of other spend (an agent's claim check) use
        it directly, with a key of their own, and pair it with :meth:`record_key`. Thread-safe.
        """
        with self._mu:
            return self._admit(key, known)

    def _admit(self, key: Hashable, known: int | None) -> Admission:
        b = self._budget
        now = self._clock()
        self._refill(now)
        if b.max_probes is not None and self.probes >= b.max_probes:
            return "over_total"
        if b.max_seconds is not None and now - self._start > b.max_seconds:
            return "over_total"
        if (
            known is not None
            and b.max_bytes_per_probe is not None
            and known > b.max_bytes_per_probe
        ):
            return "over_probe"
        committed = self._committed()
        if b.max_bytes_total is not None and committed >= b.max_bytes_total:
            return "over_total"  # nothing left (a zero total allows nothing)
        if known is not None and b.max_bytes_total is not None and known > b.max_bytes_total:
            return "over_probe"  # could never fit: not run, and not left blocking the queue
        charge = known if known is not None else self._charge_unknown()
        if (
            b.max_bytes_total is not None
            and charge is not None
            and committed + charge > b.max_bytes_total
        ):
            return "over_total"
        self.probes += 1
        if known is None:
            self.unknown_cost += 1
        self._reserved[key] = (charge, known)
        return "run"

    def record(self, result: MeasureResult) -> None:
        """Write what the probe spent to the ledger: reported bytes, else the admitted estimate,
        else the pessimistic charge (ledgered as unknown)."""
        reported = None if result.evidence is None else result.evidence.bytes_processed
        self.record_key(result.request, reported, _label(result.request))

    def record_key(
        self, key: Hashable, reported: int | None, note: str, *, kind: str = LEDGER_KIND
    ) -> None:
        """Ledger the spend of an admitted ``key`` under ``kind`` (unknown spend under
        ``<kind>.unknown_bytes``): reported bytes, else the estimate, else the charge.
        Thread-safe."""
        with self._mu:
            self._record(key, reported, note, kind)

    def _record(self, key: Hashable, reported: int | None, note: str, kind: str) -> None:
        unknown_kind = f"{kind}.unknown_bytes"
        charge, known = self._reserved.pop(key, (None, None))
        spent = reported if reported is not None else known
        if spent is not None:
            self._spend(int(spent))
            self._store.ledger_add(kind, int(spent), "bytes", note)
        else:
            self.unknown_spend += 1
            self._store.ledger_add(unknown_kind, 1, "queries", note)
            if charge is not None:
                self._spend(charge)
                self._store.ledger_add(unknown_kind, charge, "bytes", note + " (pessimistic)")
        self._store.ledger_add(kind, 1, "queries", note)

    def _spend(self, amount: int) -> None:
        self.spent_bytes += amount
        self.lifetime_spent_bytes += amount

    def remaining_bytes(self) -> int | None:
        with self._mu:
            self._refill(self._clock())
            total = self._budget.max_bytes_total
            return None if total is None else max(0, total - self._committed())
