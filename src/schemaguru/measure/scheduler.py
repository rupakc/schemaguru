"""The measurement scheduler (spec 8.2-8.6): plan -> price -> sample -> admit -> measure -> facts.

Per table: plan the probes, price each with the backend (falling back to catalog bytes,
labelled ``heuristic``), choose exact or a representative sample, admit against the budget,
measure everything admitted in one batch (so the backend can fuse), convert to facts and write
them. A second phase runs enum discovery (``TopValues``) only for columns whose measured
distinct count is at most ``enum_max_distinct``. The run stops when the total budget is spent
and reports the tables left; tables taken from the queue are put back.

An agent question never spends warehouse money by itself: the queue is drained only by an
explicit ``run(drain_queue=True)`` or by :class:`BackgroundDrainer` when the user opted in
with ``Config(measure_on_demand="background")``.
"""

from __future__ import annotations

import contextlib
import re
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal

from schemaguru._util.timeutil import utcnow
from schemaguru.config import Config
from schemaguru.diagnostics import Diagnostic, Severity
from schemaguru.measure.backend import CostEstimate, MeasureBackend, MeasureResult
from schemaguru.measure.budget import BudgetGuard
from schemaguru.measure.candidates import join_candidates
from schemaguru.measure.convert import to_facts
from schemaguru.measure.requests import (
    Freshness,
    KeyOverlapReq,
    PatternRate,
    ProfileColumns,
    Request,
    RowCount,
    TopValues,
    Uniqueness,
)
from schemaguru.measure.sampling import choose_sample, is_large, seed_for
from schemaguru.model.facts import Fact, Provenance, Status, method_class
from schemaguru.model.joins import JoinRef
from schemaguru.model.kinds import FactKind
from schemaguru.model.refs import ObjectRef
from schemaguru.model.structure import Table
from schemaguru.store.protocol import Store

TIME_FAMILIES = frozenset({"timestamp", "date"})
# probe selection only: whether text holds dates is then *measured*, never inferred from names
_DATE_TEXT_NAME = re.compile(r"(_at|_on|_ts|date|time)$", re.IGNORECASE)
DATE_TEXT_PATTERN = "____-__-__%"
DATE_TEXT_MEANS = "ISO-8601 date text"
# capability a backend reports when the dialect has no block (SYSTEM) sampling
NO_BLOCK_SAMPLING = "no_block_sampling"
_BYTES_PER_VALUE = 8  # the row-count heuristic's width of one stored value
# candidate origins whose join is only a guess: an unmeasured guess leaves no fact
_GUESSES = frozenset({"plugin", "heuristic", "llm"})
_QUEUE_ALL = 1_000_000


@dataclass
class MeasureReport:
    measured_tables: list[ObjectRef] = field(default_factory=list)
    facts_written: int = 0
    over_budget: list[ObjectRef] = field(default_factory=list)
    errors: list[Diagnostic] = field(default_factory=list)
    bytes: int = 0
    sampled_tables: list[ObjectRef] = field(default_factory=list)
    # fact-kind names this run produced a non-ERROR fact for (what it actually checked, spec 15);
    # a check that was attempted and errored is not in it
    checks_run: set[str] = field(default_factory=set)
    # unknown is not 0 (spec 25.1): ``bytes`` is a lower bound while either count is non-zero
    unknown_cost_probes: int = 0  # admitted without a known cost estimate
    unknown_spend_probes: int = 0  # ran, and neither the warehouse nor an estimate gave bytes


def _estimated(f: Fact) -> bool:
    """A fact that carries a sample's estimate (what makes a table "sampled" in a report)."""
    return method_class(f.method) == "sampled" and f.status in (
        Status.ESTIMATED,
        Status.CONTRADICTED,
    )


def _known_rows(table: Table, facts: Sequence[Fact]) -> int | None:
    """The table's row count when known exactly: a measured exact count, else catalog stats."""
    for f in facts:
        rows = f.value.get("rows")
        if (
            f.kind.name == "ROW_COUNT"
            and f.method == "exact"
            and f.status is Status.OBSERVED
            and isinstance(rows, int)
        ):
            return rows
    return table.row_count


def cap_sampled_distinct(facts: list[Fact], rows: int | None) -> list[Fact]:
    """A sampled distinct count scaled past the table's row count is capped at it (a column
    cannot hold more distinct values than rows); the estimate's bounds are capped too and the
    value says ``capped_at_rows``. Exact and unknown counts are left alone."""
    if rows is None:
        return facts
    out: list[Fact] = []
    for f in facts:
        d = f.value.get("distinct_keys")
        if f.kind.name == "DISTINCT" and _estimated(f) and isinstance(d, int) and d > rows:
            value = dict(f.value, distinct_keys=rows, capped_at_rows=True)
            est = value.get("estimate")
            if isinstance(est, dict):
                value["estimate"] = {
                    k: (min(v, rows) if k in ("value", "low", "high") and isinstance(v, int | float)
                        and not isinstance(v, bool) else v)
                    for k, v in est.items()
                }  # fmt: skip
            f = replace(f, value=value)
        out.append(f)
    return out


def _diag(code: str, message: str, location: str | None = None) -> Diagnostic:
    return Diagnostic(code, Severity.ERROR, message, location)


def _where(req: Request) -> str:
    if isinstance(req, KeyOverlapReq):
        return req.join.canonical()
    if isinstance(req, TopValues | Freshness | PatternRate):
        return req.column.canonical()
    return req.table_ref.canonical()


class _Stop(Exception):
    """The total budget is exhausted; ``ran`` says whether this phase measured anything."""

    def __init__(self, ran: bool) -> None:
        super().__init__("total budget exhausted")
        self.ran = ran


class Scheduler:
    def __init__(
        self,
        store: Store,
        backend: MeasureBackend,
        config: Config,
        *,
        now: Callable[[], datetime] = utcnow,
        snapshot_id: str | None = None,
    ) -> None:
        self.store = store
        self.backend = backend
        self.config = config
        self._now = now
        self._snapshot_id = snapshot_id
        self._labels: dict[JoinRef, str] = {}
        self._lock = threading.Lock()

    # --- planning ---------------------------------------------------------------------
    def plan_table(self, ref: ObjectRef) -> list[Request]:
        """Phase-1 probes for one table (enum discovery is planned after distinct counts)."""
        ref = ref.table_ref()
        table = self.store.table(ref)
        if table is None:
            return []
        cols = self.store.columns(ref)
        reqs: list[Request] = []
        if self._large(table):
            reqs.append(RowCount(ref))  # the sampled profile cannot give an exact row count
        reqs.append(ProfileColumns(ref, tuple(c.ref.column or "" for c in cols)))
        for con in self.store.constraints(ref):
            if con.kind == "primary_key":
                reqs.append(Uniqueness(ref, con.columns))
        for c in cols:
            if c.family in TIME_FAMILIES:
                reqs.append(Freshness(c.ref))
            elif c.family == "string" and _DATE_TEXT_NAME.search(c.ref.column or ""):
                reqs.append(PatternRate(c.ref, DATE_TEXT_PATTERN, DATE_TEXT_MEANS))
        for join, label in join_candidates(self.store, ref):
            self._labels[join] = label
            reqs.append(KeyOverlapReq(join))
        return reqs

    def _large(self, table: Table) -> bool:
        return self.config.sampling.measurement == "force" or (
            self.config.sampling.measurement != "off" and is_large(table, self.config.sampling)
        )

    # --- running ----------------------------------------------------------------------
    def run(
        self,
        refs: Sequence[ObjectRef] | None = None,
        *,
        drain_queue: bool = False,
        guard: BudgetGuard | None = None,
        config: Config | None = None,
    ) -> MeasureReport:
        """Measure ``refs`` (default: every live table, or only the queue with ``drain_queue``).

        ``guard`` shares one byte budget across runs (the background drainer); by default each
        run gets a fresh ``Budget``. ``config`` overrides the scheduler's config for this run only
        (e.g. exact measurement), under the same lock, so overrides never run alongside other
        runs. On the total budget, and on any unexpected exception, the tables not yet measured
        are pushed back onto the queue (then the exception propagates).
        """
        with self._lock:
            if config is None:
                return self._run(refs, drain_queue, guard)
            saved, self.config = self.config, config
            try:
                return self._run(refs, drain_queue, guard)
            finally:
                self.config = saved

    def reconfigure(self, config: Config, backend: MeasureBackend | None = None) -> None:
        """Replace the config (and backend) between runs: waits for a running run to finish."""
        with self._lock:
            self.config = config
            if backend is not None:
                self.backend = backend

    def _run(
        self, refs: Sequence[ObjectRef] | None, drain_queue: bool, shared: BudgetGuard | None
    ) -> MeasureReport:
        report = MeasureReport()
        guard = shared if shared is not None else BudgetGuard(self.config.budget, self.store)
        guard.new_run()
        spent0, ucost0, uspend0 = (
            guard.lifetime_spent_bytes,
            guard.unknown_cost,
            guard.unknown_spend,
        )
        snapshot = self._snapshot_id or self.store.latest_snapshot() or "live"
        targets: list[ObjectRef] = [r.table_ref() for r in refs] if refs is not None else []
        reasons: dict[ObjectRef, str] = {}
        if drain_queue:
            for ref, reason in self.store.queue_pop(_QUEUE_ALL):
                targets.append(ref)
                reasons.setdefault(ref, reason)
        elif refs is None:
            targets = [t.ref for t in self.store.iter_tables()]
        targets = list(dict.fromkeys(targets))

        def requeue(left: Sequence[ObjectRef], why: str) -> None:
            for t in left:
                self.store.queue_push(t, reasons.get(t, why), 0.0)

        try:
            for i, ref in enumerate(targets):
                try:
                    self._run_table(ref, guard, snapshot, report)
                except _Stop:
                    report.over_budget = targets[i:]
                    report.errors.append(
                        _diag(
                            "SG302",
                            f"total measurement budget exhausted; {len(report.over_budget)} "
                            "table(s) left in the queue",
                        )
                    )
                    requeue(report.over_budget, "over_budget")
                    break
                except BaseException:
                    requeue(targets[i:], "interrupted")  # nothing popped is ever lost
                    raise
        finally:
            report.bytes = guard.lifetime_spent_bytes - spent0
            report.unknown_cost_probes = guard.unknown_cost - ucost0
            report.unknown_spend_probes = guard.unknown_spend - uspend0
        return report

    def _run_table(
        self, ref: ObjectRef, guard: BudgetGuard, snapshot: str, report: MeasureReport
    ) -> None:
        table = self.store.table(ref)
        if table is None or table.withdrawn_at is not None:
            report.errors.append(
                _diag("SG305", "table is not in the store (crawl it first)", ref.canonical())
            )
            return
        observed_at = self._now()
        seed = seed_for(ref, snapshot)
        facts: list[Fact] = []
        ran = False
        try:
            results, ran = self._phase(
                self.plan_table(ref), table, guard, seed, observed_at, snapshot, facts, report
            )
            facts += self._convert(results, observed_at, snapshot, report)
            enum = self._enum_requests(facts)
            if enum:
                more, ran2 = self._phase(
                    enum, table, guard, seed, observed_at, snapshot, facts, report
                )
                ran = ran or ran2
                facts += self._convert(more, observed_at, snapshot, report)
        except _Stop as stop:
            ran = ran or stop.ran
            raise
        finally:
            facts = cap_sampled_distinct(facts, _known_rows(table, facts))
            if ref in report.sampled_tables and not any(_estimated(f) for f in facts):
                report.sampled_tables.remove(ref)  # nothing estimated came of the sample
            if facts:
                self.store.put_facts(facts)
                report.facts_written += len(facts)
                report.checks_run |= {f.kind.name for f in facts if f.status is not Status.ERROR}
            if ran:
                report.measured_tables.append(ref)

    def _phase(
        self,
        reqs: list[Request],
        table: Table,
        guard: BudgetGuard,
        seed: int,
        observed_at: datetime,
        snapshot: str,
        facts: list[Fact],
        report: MeasureReport,
    ) -> tuple[list[MeasureResult], bool]:
        """Price, sample and admit ``reqs``; measure the admitted ones in one batch."""
        admitted: list[Request] = []
        stop = False
        for req in reqs:
            if isinstance(req, RowCount) and table.row_count is not None and self._large(table):
                facts.append(
                    Fact.make(
                        FactKind.ROW_COUNT,
                        table.ref,
                        {"rows": table.row_count},
                        Provenance.CATALOG,
                        Status.OBSERVED,
                        source="catalog_stats",
                        observed_at=observed_at,
                        snapshot_id=snapshot,
                    )
                )
                continue
            req, est = self._priced(req, table, seed)
            verdict = guard.admit(est)
            if verdict == "run":
                admitted.append(req)
                if req.sample is not None and table.ref not in report.sampled_tables:
                    report.sampled_tables.append(table.ref)
            elif verdict == "over_probe":
                d = _diag(
                    "SG301",
                    f"probe over the per-probe budget ({est.bytes} bytes, {est.method}); not run",
                    _where(req),
                )
                report.errors.append(d)
                facts += self._facts_for(MeasureResult(req, None, None, d), observed_at, snapshot)
            else:
                stop = True
                break
        results = self._measure(admitted, report) if admitted else []
        for r in results:
            guard.record(r)
        if stop:
            facts += self._convert(results, observed_at, snapshot, report)
            raise _Stop(bool(results))
        return results, bool(results)

    def _measure(self, reqs: list[Request], report: MeasureReport) -> list[MeasureResult]:
        try:
            results = self.backend.measure(reqs)
            if len(results) != len(reqs):
                raise RuntimeError(f"backend returned {len(results)} results for {len(reqs)}")
        except Exception as e:  # one bad table costs one table
            d = _diag("SG303", f"measurement backend failed: {e}")
            return [MeasureResult(r, None, None, d) for r in reqs]
        return results

    def _convert(
        self,
        results: list[MeasureResult],
        observed_at: datetime,
        snapshot: str,
        report: MeasureReport,
    ) -> list[Fact]:
        out: list[Fact] = []
        seen: set[int] = set()
        for r in results:
            if r.error is not None and id(r.error) not in seen:
                seen.add(id(r.error))
                report.errors.append(
                    r.error if r.error.location else replace(r.error, location=_where(r.request))
                )
            out += self._facts_for(r, observed_at, snapshot)
        return out

    def _facts_for(self, r: MeasureResult, observed_at: datetime, snapshot: str) -> list[Fact]:
        req = r.request
        if (
            r.error is not None
            and isinstance(req, KeyOverlapReq)
            and self._labels.get(req.join) in _GUESSES
        ):
            return []  # an unmeasured guess is not a join: no fact, only the diagnostic
        return to_facts(r, observed_at=observed_at, snapshot_id=snapshot, config=self.config)

    # --- pricing and sampling ---------------------------------------------------------
    def _priced(self, req: Request, table: Table, seed: int) -> tuple[Request, CostEstimate]:
        est = self._estimate(req, table)
        basis = self._sampling_table(req, table)
        choice = choose_sample(
            basis,
            req,
            self.config.sampling,
            key_distinct=self._key_distinct(req),
            est_bytes=est.bytes,
            max_bytes_per_probe=self.config.budget.max_bytes_per_probe,
            seed=seed,
            block_sampling=NO_BLOCK_SAMPLING not in self.backend.capabilities(),
            pk=self._primary_key(table.ref) if isinstance(req, TopValues) else None,
        )
        if choice is None:
            return req, est
        sampled = replace(req, sample=choice)
        sest = self._estimate(sampled, table)
        if sest.bytes is None and est.bytes is not None:
            factor = choice.fraction if choice.method == "system" else Decimal(1)
            sest = CostEstimate(
                sampled,
                int(Decimal(est.bytes) * factor),
                "row_estimate" if est.method == "row_estimate" else "heuristic",
            )
        return sampled, sest

    def _estimate(self, req: Request, table: Table) -> CostEstimate:
        try:
            (est,) = self.backend.estimate([req])
        except Exception:
            est = CostEstimate(req, None, "unknown")
        if est.bytes is not None:
            return est
        heuristic = self._catalog_bytes(req, table)
        if heuristic is not None:
            return CostEstimate(req, heuristic, "heuristic")
        rows = self._row_estimate(req, table)
        if rows is not None:
            return CostEstimate(req, rows, "row_estimate")
        return CostEstimate(req, None, "unknown")

    def _catalog_bytes(self, req: Request, table: Table) -> int | None:
        if not isinstance(req, KeyOverlapReq):
            return table.bytes
        total = 0
        for side in (req.join.left, req.join.right):
            t = self.store.table(side[0].table_ref())
            if t is None or t.bytes is None:
                return None
            total += t.bytes
        return total

    def _row_estimate(self, req: Request, table: Table) -> int | None:
        """``rows x columns read x 8`` from catalog row counts (spec 8.3), for warehouses with
        neither a dry-run nor table bytes (DuckDB). Only the columns the probe reads count;
        a bare row count reads one. None when a row count is missing."""
        if isinstance(req, KeyOverlapReq):
            total = 0
            for side in (req.join.left, req.join.right):
                t = self.store.table(side[0].table_ref())
                if t is None or t.row_count is None:
                    return None
                total += t.row_count * len(side) * _BYTES_PER_VALUE
            return total
        if table.row_count is None:
            return None
        # TopValues, Freshness, PatternRate read one column; a bare RowCount is counted as one
        ncols = (len(req.columns) or 1) if isinstance(req, ProfileColumns | Uniqueness) else 1
        return table.row_count * ncols * _BYTES_PER_VALUE

    def _sampling_table(self, req: Request, table: Table) -> Table:
        """For a join the larger side decides whether to sample."""
        if not isinstance(req, KeyOverlapReq):
            return table
        sides = [self.store.table(s[0].table_ref()) for s in (req.join.left, req.join.right)]
        known = [t for t in sides if t is not None]
        if not known:
            return table
        return max(known, key=lambda t: (t.bytes or 0, t.row_count or 0))

    def _primary_key(self, ref: ObjectRef) -> tuple[str, ...] | None:
        """A catalog primary key, else a key measured unique and complete; else None."""
        for con in self.store.constraints(ref):
            if con.kind == "primary_key" and con.columns:
                return tuple(con.columns)
        for f in self.store.facts_for([ref], ["PRIMARY_KEY"]):
            cols = f.value.get("columns")
            if (
                f.provenance is Provenance.MEASURED
                and f.status is Status.OBSERVED
                and f.value.get("unique") is True
                and f.value.get("complete") is True
                and isinstance(cols, list)
                and cols
            ):
                return tuple(str(c) for c in cols)
        return None

    def _key_distinct(self, req: Request) -> int | None:
        if isinstance(req, KeyOverlapReq):
            if len(req.join.left) != 1:
                return None
            cols = [req.join.left[0], req.join.right[0]]
        elif isinstance(req, Uniqueness) and len(req.columns) == 1:
            t = req.table
            cols = [ObjectRef(t.catalog, t.schema, t.table, req.columns[0])]
        else:
            return None
        best: int | None = None
        for f in self.store.facts_for(cols, ["DISTINCT"]):
            v = f.value.get("distinct_keys")
            if f.method == "exact" and f.status is Status.OBSERVED and isinstance(v, int):
                best = v if best is None else max(best, v)
        return best

    def _enum_requests(self, facts: list[Fact]) -> list[Request]:
        limit = self.config.enum_max_distinct
        out: list[Request] = []
        for f in facts:
            if f.kind.name != "DISTINCT" or not isinstance(f.subject, ObjectRef):
                continue
            if f.status not in (Status.OBSERVED, Status.ESTIMATED):
                continue
            d = f.value.get("distinct_keys")
            if isinstance(d, int) and 0 < d <= limit:
                out.append(TopValues(f.subject, limit))
        return out


class BackgroundDrainer:
    """One daemon thread that drains the queue under the configured budget (spec 8.2).

    Used only when the user opted in with ``Config(measure_on_demand="background")``.
    """

    MAX_BACKOFF = 64  # the wait after repeated SG302 is at most interval_s * MAX_BACKOFF

    def __init__(
        self,
        scheduler: Scheduler,
        interval_s: float = 5.0,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if scheduler.config.measure_on_demand != "background":
            raise ValueError("BackgroundDrainer needs Config(measure_on_demand='background')")
        self.scheduler = scheduler
        self.interval_s = interval_s
        self.wait_s = interval_s
        # one byte budget for the drainer's whole life: never max_bytes_total per loop
        # (refilled once per Budget.period_s)
        self.guard = BudgetGuard(scheduler.config.budget, scheduler.store, clock=clock)
        self.last_report: MeasureReport | None = None
        self.last_error: BaseException | None = None
        # called with each failure of a drain (the host turns it into a diagnostic)
        self.on_error: Callable[[BaseException], None] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="schemaguru-measure", daemon=True)
        self._thread.start()

    def run_once(self) -> MeasureReport:
        """One drain of the queue on the shared budget; backs off after SG302."""
        report = self.scheduler.run(drain_queue=True, guard=self.guard)
        self.last_report = report
        if any(d.code == "SG302" for d in report.errors):
            self.wait_s = min(self.wait_s * 2, self.interval_s * self.MAX_BACKOFF)
        else:
            self.wait_s = self.interval_s
        return report

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as e:  # keep draining; the error is kept for the host to read
                self.last_error = e
                self.wait_s = min(self.wait_s * 2, self.interval_s * self.MAX_BACKOFF)
                hook = self.on_error
                if hook is not None:
                    with contextlib.suppress(Exception):  # a host hook never stops the drainer
                        hook(e)
            self._stop.wait(self.wait_s)

    def stop(self, timeout: float | None = 10.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()
