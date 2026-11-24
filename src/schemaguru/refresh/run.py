"""Incremental refresh with relative drift classification (spec 15)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from schemaguru._util.timeutil import utcnow
from schemaguru.catalog.crawl import crawl
from schemaguru.catalog.protocol import CatalogReader
from schemaguru.config import Config
from schemaguru.knowledge.diff import Change, diff
from schemaguru.knowledge.resolve import resolve
from schemaguru.knowledge.snapshot import take
from schemaguru.measure.scheduler import MeasureReport, Scheduler
from schemaguru.model.facts import Fact, Provenance
from schemaguru.model.joins import JoinRef
from schemaguru.model.refs import ObjectRef
from schemaguru.refresh.classify import (
    CATALOG_CHECK,
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
    declared/human expectations (PRIMARY_KEY, VALUES, JOIN) on the touched tables. Exit code:
    ``3`` when measurement was attempted and nothing was measured, else ``1`` if any change needs
    review, else ``0``. Nothing to measure (everything fresh) is a clean ``0``.
    """
    t = now()
    prev_id = store.latest_snapshot()
    prev_facts = store.snapshot_facts(prev_id) if prev_id else []
    live_before = {x.ref for x in store.iter_tables()}

    crawl_report = crawl(reader, store, config=config.crawl, incremental=True, now=now)
    withdrawn = sorted(
        (r for r in live_before if (tb := store.table(r)) is not None and tb.withdrawn_at),
        key=lambda r: r.canonical(),
    )

    targets = _stale_or_changed(store, t, config.fact_ttl_days)
    measure = scheduler.run(targets) if targets else MeasureReport()

    checks_run = set(measure.checks_run)
    if not crawl_report.errors or crawl_report.tables_seen > 0:
        checks_run.add(CATALOG_CHECK)
    snap = take(store, checks_run, label)
    new_facts = store.snapshot_facts(snap)

    changes: list[Change] = diff(prev_facts, new_facts)
    changes += [Change(TABLE_WITHDRAWN, r, None, None, "removed") for r in withdrawn]
    prev_eff = resolve(prev_facts, now=t, ttl_days=config.fact_ttl_days)
    new_eff = resolve(new_facts, now=t, ttl_days=config.fact_ttl_days)
    classified = classify_changes(
        changes, prev_eff, new_eff, checks_run=checks_run, measured_tables=measure.measured_tables
    )

    if targets and not measure.measured_tables:
        code = EXIT_NOTHING_MEASURED
    elif any(c is ChangeClass.REVIEW for _, c in classified):
        code = EXIT_REVIEW
    else:
        code = EXIT_OK
    return RefreshReport(
        classified, crawl_report, measure, code, snap, prev_id, frozenset(checks_run)
    )
