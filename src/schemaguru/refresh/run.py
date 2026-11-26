"""Incremental refresh with relative drift classification (spec 15)."""

from __future__ import annotations

from collections.abc import Callable, Collection
from datetime import datetime, timedelta

from schemaguru._util.timeutil import utcnow
from schemaguru.catalog.crawl import CrawlReport, crawl
from schemaguru.catalog.protocol import CatalogReader
from schemaguru.config import Config
from schemaguru.diagnostics import Diagnostic, Severity
from schemaguru.knowledge.diff import Change, diff_split, subject_key
from schemaguru.knowledge.resolve import resolve
from schemaguru.knowledge.snapshot import take
from schemaguru.measure.scheduler import MeasureReport, Scheduler
from schemaguru.model.facts import Fact, Provenance, Status
from schemaguru.model.joins import JoinRef
from schemaguru.model.refs import ObjectRef
from schemaguru.refresh.classify import (
    CATALOG_CHECK,
    MEASURED_KINDS,
    TABLE_WITHDRAWN,
    ChangeClass,
    classify_changes,
)
from schemaguru.refresh.report import (
    EXIT_NOTHING_MEASURED,
    EXIT_OK,
    EXIT_REVIEW,
    RefreshReport,
)
from schemaguru.store.protocol import Store


def _tables_of(f: Fact) -> set[ObjectRef]:
    s = f.subject
    if isinstance(s, JoinRef):
        return {x.table_ref() for x in (*s.left, *s.right)}
    return {s.table_ref()}


def _stale_or_changed(store: Store, now: datetime, ttl_days: int) -> list[ObjectRef]:
    """Live tables never measured, altered since their newest measurement, or past the TTL."""
    newest: dict[ObjectRef, datetime] = {}
    for f in store.iter_facts():
        if f.provenance is not Provenance.MEASURED or f.observed_at is None:
            continue
        for t in _tables_of(f):
            if t not in newest or f.observed_at > newest[t]:
                newest[t] = f.observed_at
    cutoff = now - timedelta(days=ttl_days)
    out: list[ObjectRef] = []
    for tb in store.iter_tables():
        seen = newest.get(tb.ref)
        if (
            seen is None
            or seen < cutoff
            or (tb.last_altered is not None and tb.last_altered > seen)
        ):
            out.append(tb.ref)
    return out


STATE_KEY = "refresh"
CARRIED_PREFIX = "carried:"  # snapshot checks_run entry for a kind that was carried, not run


def _wkey(ref: ObjectRef, at: datetime) -> str:
    return f"{ref.canonical()}|{at.isoformat()}"


def _reported(store: Store) -> set[str]:
    raw = (store.crawl_state_get(STATE_KEY) or {}).get("withdrawn_reported")
    return {str(x) for x in raw} if isinstance(raw, list) else set()


def _currently_withdrawn_keys(store: Store, prev_facts: list[Fact]) -> dict[str, ObjectRef]:
    """``name|withdrawn_at`` -> table, for tables the previous snapshot refers to."""
    seen: dict[str, ObjectRef] = {}
    for f in prev_facts:
        for r in _tables_of(f):
            seen[r.canonical()] = r
    out: dict[str, ObjectRef] = {}
    for key in sorted(seen):
        tb = store.table(seen[key])
        if tb is not None and tb.withdrawn_at is not None:
            out[_wkey(seen[key], tb.withdrawn_at)] = seen[key]
    return out


def _withdrawn_since_snapshot(store: Store, prev_facts: list[Fact]) -> list[ObjectRef]:
    """Withdrawn tables the previous snapshot's facts refer to that are not yet reported.

    Compared against the previous snapshot (not against this run's crawl), and keyed on
    ``(name, withdrawn_at)``: a table restored and dropped again, even between two direct
    ``crawl()`` calls, is a new withdrawal. The reported set is saved only at the very end of a
    run (see :func:`refresh`), so a run that fails before returning its report loses nothing.
    """
    reported = _reported(store)
    return [r for k, r in _currently_withdrawn_keys(store, prev_facts).items() if k not in reported]


def _carried_unmeasured(
    prev_facts: list[Fact],
    new_facts: list[Fact],
    targets: Collection[ObjectRef],
    catalog_ran: bool,
    withdrawn: Collection[ObjectRef] = (),
) -> list[Change]:
    """Measured facts carried unchanged from the previous snapshot on tables that were due.

    The store never deletes facts, so a narrower run leaves the old ones in place and a plain
    diff sees nothing. A carried MEASURED fact (same id, ``observed_at`` not advanced) on a DUE
    table is not measured, whether its kind ran on other tables or not, with three exceptions:
    a fact is superseded, not missing, when ANY fact of the same (kind, subject) was re-stamped
    this run, whatever its method class (exact, sampled or error: ``method_class`` is part of the
    id, so a table that switched to sampling or a probe that recovered leaves the old fact
    behind); a carried ERROR fact is never reported; and facts on a withdrawn table are covered by
    ``TABLE_WITHDRAWN``. A table that was not due is never reported (its facts are still fresh).
    Carried CATALOG facts are reported only when this run's crawl had errors.
    """
    prev_at = {f.id: f.observed_at for f in prev_facts}
    due, gone = set(targets), set(withdrawn)
    restamped = {
        (f.kind.name, subject_key(f.subject))
        for f in new_facts
        if f.id not in prev_at or f.observed_at != prev_at[f.id]
    }
    out: list[Change] = []
    for f in new_facts:
        if f.id not in prev_at or f.observed_at != prev_at[f.id]:
            continue
        if f.provenance is Provenance.CATALOG:
            skipped = not catalog_ran
        elif f.provenance is Provenance.MEASURED and f.kind.name in MEASURED_KINDS:
            tables = _tables_of(f)
            skipped = (
                bool(tables & due)
                and not tables & gone
                and f.status is not Status.ERROR
                and (f.kind.name, subject_key(f.subject)) not in restamped
            )
        else:
            continue
        if skipped:
            out.append(Change(f.kind, f.subject, f, None, "removed"))
    return out


def _carried_kinds(
    prev_facts: list[Fact], targets: Collection[ObjectRef], now: datetime, ttl_days: int
) -> set[str]:
    """Kinds still standing from tables that were not due: their facts are fresh (within TTL)."""
    due, cutoff = set(targets), now - timedelta(days=ttl_days)
    return {
        f.kind.name
        for f in prev_facts
        if f.provenance is Provenance.MEASURED
        and f.kind.name in MEASURED_KINDS
        and f.observed_at is not None
        and f.observed_at >= cutoff
        and not _tables_of(f) & due
    }


def _error(code: str, message: str) -> Diagnostic:
    return Diagnostic(code, Severity.ERROR, message)


def refresh(
    store: Store,
    reader: CatalogReader,
    scheduler: Scheduler,
    *,
    config: Config,
    now: Callable[[], datetime] = utcnow,
    label: str | None = None,
) -> RefreshReport:
    """Crawl incrementally, re-measure stale/changed tables, snapshot, diff and classify.

    Re-measuring a table re-runs every probe the scheduler plans for it, which re-checks the
    declared/human expectations (PRIMARY_KEY, VALUES, JOIN) on the touched tables. A reader or
    scheduler failure (``Exception``) is recorded in the report, never raised. The report carries
    two sets: ``run_checks`` (what this run measured without error; a check that was attempted and
    errored is not in it, and ``CATALOG`` only for a crawl without errors) and ``carried_checks``
    (kinds still fresh on tables that were not due). The snapshot stores both (carried kinds are
    prefixed ``carried:``). Any carried measured fact on a table that was due is NOT_MEASURED, and
    ``missing_checks`` lists the kinds of those facts.

    Exit code: ``3`` when the crawl failed with nothing seen, any measurement error was reported,
    or measurement was attempted and nothing was measured; else ``1`` if any change needs review;
    else ``0``. Nothing to measure (everything fresh) is a clean ``0``.
    """
    t = now()
    prev_id = store.latest_snapshot()
    prev_facts = store.snapshot_facts(prev_id) if prev_id else []

    try:
        crawl_report = crawl(
            reader,
            store,
            config=config.crawl,
            incremental=True,
            now=now,
            crawl_bytes=config.budget.crawl_bytes,
        )
    except Exception as e:
        crawl_report = CrawlReport(0, 0, 0, [_error("SG102", f"crawl failed: {e}")])
    withdrawn = _withdrawn_since_snapshot(store, prev_facts)

    targets = _stale_or_changed(store, t, config.fact_ttl_days)
    measure = MeasureReport()
    if targets:
        try:
            measure = scheduler.run(targets)
        except Exception as e:
            measure = MeasureReport(errors=[_error("SG303", f"measurement failed: {e}")])

    run_checks = set(measure.checks_run)  # what THIS run measured without error
    if not crawl_report.errors:
        run_checks.add(CATALOG_CHECK)
    # kinds still standing on tables that were not due (fresh facts); recorded apart, never mixed
    carried_checks = _carried_kinds(prev_facts, targets, t, config.fact_ttl_days)
    snap = take(store, run_checks | {CARRIED_PREFIX + k for k in carried_checks}, label)
    new_facts = store.snapshot_facts(snap)

    # a fact that vanished while its check did not run was not looked at: diff leaves it out of
    # the removals, and it is reported NOT_MEASURED (never "fixed", base spec 15)
    changes, unlooked = diff_split(
        prev_facts, new_facts, checks_run=store.snapshot_checks_run(snap)
    )
    changes += [Change(TABLE_WITHDRAWN, r, None, None, "removed") for r in withdrawn]
    carried = _carried_unmeasured(
        prev_facts,
        new_facts,
        targets,
        CATALOG_CHECK in run_checks,
        list(_currently_withdrawn_keys(store, prev_facts).values()),
    )
    missing = sorted(
        {
            CATALOG_CHECK if c.before and c.before.provenance is Provenance.CATALOG else c.kind.name
            for c in carried
        }
    )
    prev_eff = resolve(prev_facts, now=t, ttl_days=config.fact_ttl_days)
    new_eff = resolve(new_facts, now=t, ttl_days=config.fact_ttl_days)
    classified = classify_changes(
        changes, prev_eff, new_eff, checks_run=run_checks, measured_tables=measure.measured_tables
    )
    classified += [(c, ChangeClass.NOT_MEASURED) for c in [*unlooked, *carried]]

    nothing_seen = bool(crawl_report.errors) and crawl_report.tables_seen == 0
    if nothing_seen or measure.errors or (targets and not measure.measured_tables):
        code = EXIT_NOTHING_MEASURED
    elif any(c is ChangeClass.REVIEW for _, c in classified):
        code = EXIT_REVIEW
    else:
        code = EXIT_OK
    report = RefreshReport(
        classified,
        crawl_report,
        measure,
        code,
        snap,
        prev_id,
        frozenset(run_checks),
        missing,
        frozenset(carried_checks),
    )
    # last, after diff/resolve/classify: a failure above must not mark a withdrawal reported
    store.crawl_state_set(
        STATE_KEY,
        {"withdrawn_reported": sorted(_currently_withdrawn_keys(store, prev_facts))},
    )
    return report
