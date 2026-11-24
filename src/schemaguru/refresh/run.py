"""Incremental refresh with relative drift classification (spec 15)."""

from __future__ import annotations

from collections.abc import Callable, Collection
from datetime import datetime, timedelta

from schemaguru._util.timeutil import utcnow
from schemaguru.catalog.crawl import CrawlReport, crawl
from schemaguru.catalog.protocol import CatalogReader
from schemaguru.config import Config
from schemaguru.diagnostics import Diagnostic, Severity
from schemaguru.knowledge.diff import Change, diff
from schemaguru.knowledge.resolve import resolve
from schemaguru.knowledge.snapshot import take
from schemaguru.measure.scheduler import MeasureReport, Scheduler
from schemaguru.model.facts import Fact, Provenance
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


def _reported(store: Store) -> set[str]:
    raw = (store.crawl_state_get(STATE_KEY) or {}).get("withdrawn_reported")
    return {str(x) for x in raw} if isinstance(raw, list) else set()


def _still_withdrawn(store: Store, keys: Collection[str]) -> list[str]:
    out = []
    for k in sorted(keys):
        tb = store.table(ObjectRef.parse(k))
        if tb is not None and tb.withdrawn_at is not None:
            out.append(k)  # a restored table drops out, so a later withdrawal reports again
    return out


def _withdrawn_since_snapshot(store: Store, prev_facts: list[Fact]) -> list[ObjectRef]:
    """Tables the previous snapshot's facts refer to that are withdrawn now and not yet reported.

    Compared against the previous snapshot (not against the crawl of this run), so a run that
    crashed between crawl and snapshot loses nothing: the withdrawal stays unreported until a
    snapshot has been taken (the reported set is saved only after that).
    """
    reported = _reported(store)
    seen: dict[str, ObjectRef] = {}
    for f in prev_facts:
        for r in _tables_of(f):
            seen[r.canonical()] = r
    out = []
    for key in sorted(seen):
        tb = store.table(seen[key])
        if tb is not None and tb.withdrawn_at is not None and key not in reported:
            out.append(seen[key])
    return out


def _carried_unmeasured(
    prev_facts: list[Fact],
    new_facts: list[Fact],
    prev_checks: Collection[str],
    checks_run: Collection[str],
    targets: Collection[ObjectRef],
    measured: Collection[ObjectRef],
) -> list[Change]:
    """Facts carried unchanged from the previous snapshot whose check did not run this time.

    The store never deletes facts, so a narrower run leaves the old ones in place and a plain
    diff sees nothing. A carried fact (same id, ``observed_at`` not advanced) is reported as
    not measured when its kind was checked last time and not now, or its table was due for
    measurement and was not reached. Tables that were not due (still fresh) are not reported.
    """
    prev_at = {f.id: f.observed_at for f in prev_facts}
    due, done = set(targets), set(measured)
    out: list[Change] = []
    for f in new_facts:
        if f.id not in prev_at or f.observed_at != prev_at[f.id]:
            continue
        if f.provenance is Provenance.CATALOG:
            skipped = CATALOG_CHECK in prev_checks and CATALOG_CHECK not in checks_run
        elif f.provenance is Provenance.MEASURED and f.kind.name in MEASURED_KINDS:
            tables = _tables_of(f)
            if not tables & due:
                continue
            skipped = (f.kind.name in prev_checks and f.kind.name not in checks_run) or not (
                tables & done
            )
        else:
            continue
        if skipped:
            out.append(Change(f.kind, f.subject, f, None, "removed"))
    return out


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
    scheduler failure (``Exception``) is recorded in the report, never raised. A check that was
    attempted and errored is not in ``checks_run``; ``CATALOG`` is only there for a crawl without
    errors. When nothing was due for measurement the previous measured checks still stand
    (everything is fresh), so they carry into ``checks_run``.

    Exit code: ``3`` when the crawl failed with nothing seen, any measurement error was reported,
    or measurement was attempted and nothing was measured; else ``1`` if any change needs review;
    else ``0``. Nothing to measure (everything fresh) is a clean ``0``.
    """
    t = now()
    prev_id = store.latest_snapshot()
    prev_facts = store.snapshot_facts(prev_id) if prev_id else []
    prev_checks = store.snapshot_checks_run(prev_id) if prev_id else frozenset()

    try:
        crawl_report = crawl(reader, store, config=config.crawl, incremental=True, now=now)
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

    checks_run = set(measure.checks_run)
    if not targets:
        checks_run |= set(prev_checks) - {CATALOG_CHECK}
    if not crawl_report.errors:
        checks_run.add(CATALOG_CHECK)
    missing = sorted(set(prev_checks) - checks_run)
    snap = take(store, checks_run, label)
    new_facts = store.snapshot_facts(snap)
    store.crawl_state_set(
        STATE_KEY,
        {
            "withdrawn_reported": _still_withdrawn(
                store, _reported(store) | {r.canonical() for r in withdrawn}
            )
        },
    )

    changes: list[Change] = diff(prev_facts, new_facts)
    changes += [Change(TABLE_WITHDRAWN, r, None, None, "removed") for r in withdrawn]
    changes += _carried_unmeasured(
        prev_facts, new_facts, prev_checks, checks_run, targets, measure.measured_tables
    )
    prev_eff = resolve(prev_facts, now=t, ttl_days=config.fact_ttl_days)
    new_eff = resolve(new_facts, now=t, ttl_days=config.fact_ttl_days)
    classified = classify_changes(
        changes, prev_eff, new_eff, checks_run=checks_run, measured_tables=measure.measured_tables
    )

    nothing_seen = bool(crawl_report.errors) and crawl_report.tables_seen == 0
    if nothing_seen or measure.errors or (targets and not measure.measured_tables):
        code = EXIT_NOTHING_MEASURED
    elif any(c is ChangeClass.REVIEW for _, c in classified):
        code = EXIT_REVIEW
    else:
        code = EXIT_OK
    return RefreshReport(
        classified, crawl_report, measure, code, snap, prev_id, frozenset(checks_run), missing
    )
