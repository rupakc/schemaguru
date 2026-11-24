"""Error intake (spec 16.1, 25.10): identifiers and counts only; never raises."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from schemaguru.feedback.classifiers import (
    BUILTIN_CLASSIFIERS,
    FailureClassifier,
    classify_all,
)
from schemaguru.model.refs import ObjectRef
from schemaguru.store.protocol import Store

_IDENT = re.compile(r"[A-Za-z_][\w$]{0,127}\Z")


@dataclass(frozen=True)
class ReportOutcome:
    written: int
    skipped: int  # classified but not attributable to a stored table (or column)


def _resolve_table(
    store: Store, ref: ObjectRef, cache: dict[ObjectRef, ObjectRef | None]
) -> ObjectRef | None:
    if ref in cache:
        return cache[ref]
    found, cands = store.resolve(ref.canonical())
    out: ObjectRef | None = found
    if found is None and not cands and ref.catalog is None:
        # the SQL may omit the schema: accept a unique stored table whose trailing parts match
        want = [p.casefold() for p in (ref.schema, ref.table) if p is not None]
        hits = []
        for t in store.iter_tables():
            have = [p.casefold() for p in (t.ref.schema, t.ref.table) if p is not None]
            if have[-len(want) :] == want:
                hits.append(t.ref.table_ref())
                if len(hits) > 1:
                    break
        out = hits[0] if len(hits) == 1 else None
    cache[ref] = out
    return out


def _has_column(store: Store, table: ObjectRef, column: str) -> bool:
    return any(
        c.ref.column is not None and c.ref.column.casefold() == column.casefold()
        for c in store.columns(table)
    )


def report_error_detailed(
    store: Store,
    sql: str,
    error: str,
    dialect: str | None = None,
    *,
    classifiers: Sequence[FailureClassifier] | None = None,
) -> ReportOutcome:
    written = skipped = 0
    try:
        records = classify_all(sql, error, dialect, classifiers)
        cache: dict[ObjectRef, ObjectRef | None] = {}
        seen: set[tuple[str, str | None, str]] = set()
        for rec in records:
            try:
                table = _resolve_table(store, rec.table, cache) if rec.table is not None else None
                if table is None:
                    skipped += 1
                    continue
                col = rec.column
                if col is not None and not _IDENT.match(col):
                    skipped += 1
                    continue
                if col is not None and rec.failure_class != "unknown_column":
                    # ambiguity / type errors name a column that must exist on the table
                    if not _has_column(store, table, col):
                        skipped += 1
                        continue
                    col = next(
                        c.ref.column
                        for c in store.columns(table)
                        if c.ref.column is not None and c.ref.column.casefold() == col.casefold()
                    )
                key = (table.canonical(), col, rec.failure_class)
                if key in seen:
                    continue
                seen.add(key)
                store.feedback_add(table, col, rec.failure_class)
                # +1.0 per failure: the queue keeps the MAX of pushes, so push the running total
                # of this table's failures of this class (the feedback log is the counter)
                total = sum(
                    int(str(r["count"]))
                    for r in store.feedback_summary()
                    if r["table"] == table.canonical() and r["failure_class"] == rec.failure_class
                )
                store.queue_push(table, f"feedback:{rec.failure_class}", float(total))
                written += 1
            except Exception:
                skipped += 1
    except Exception:
        pass
    return ReportOutcome(written, skipped)


def report_error(
    store: Store,
    sql: str,
    error: str,
    dialect: str | None = None,
    *,
    classifiers: Sequence[FailureClassifier] | None = None,
) -> int:
    """Record feedback for one failed query; returns the number of records written."""
    return report_error_detailed(store, sql, error, dialect, classifiers=classifiers).written


__all__ = ["BUILTIN_CLASSIFIERS", "ReportOutcome", "report_error", "report_error_detailed"]
