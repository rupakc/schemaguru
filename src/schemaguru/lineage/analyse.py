"""Definition analysis: SQL definitions through the parser worker to lineage facts
(0.2.0 spec 4.1, 4.4).

The raw SQL lives only in the :class:`DefinitionItem` passed in. A result keeps its sha256 and,
on success, the stored-safe template the caller's ``template`` callable makes of it (crawl and
dbt pass :func:`schemaguru.pii.mask.definition_template`; ``lineage`` imports nothing from
``pii``). Every fact is ``UNVERIFIED``: lineage is parsed, never measured.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from schemaguru.diagnostics import Diagnostic, Severity
from schemaguru.lineage.parse import FailureReason, LineageFailure, ParsedLineage
from schemaguru.lineage.worker import ExtractRequest, LineageRequest, ParserPool
from schemaguru.model.evidence import Evidence
from schemaguru.model.facts import Fact, Provenance, Status
from schemaguru.model.kinds import FactKind
from schemaguru.model.lineage import (
    RawName,
    ResolvedTable,
    column_lineage_value,
    table_lineage_value,
)
from schemaguru.model.refs import ObjectRef


@dataclass(frozen=True)
class DefinitionItem:
    target: ObjectRef
    sql: str
    dialect: str


@dataclass(frozen=True)
class DefinitionResult:
    """``parsed`` xor ``failure``; ``template`` is set only with ``parsed``. ``names`` are the
    relation names the definition reads (as written), set only with ``parsed``: a caller keeps
    them to tell later whether those names still resolve the same way."""

    target: ObjectRef
    dialect: str
    sha256: str
    parsed: ParsedLineage | None
    failure: FailureReason | None
    template: str | None
    names: tuple[RawName, ...] = ()


Resolver = Callable[[Sequence[RawName], ObjectRef], Mapping[RawName, ResolvedTable]]


def definition_sha256(sql: str) -> str:
    """The sha256 kept for a definition (to skip re-parsing an unchanged one)."""
    return hashlib.sha256(sql.encode("utf-8", "surrogatepass")).hexdigest()


def analyse_definitions(
    items: Sequence[DefinitionItem],
    *,
    resolve: Resolver,
    pool: ParserPool,
    template: Callable[[str, str], str | None],
    columns: bool = True,
) -> list[DefinitionResult]:
    """One result per item, in input order: two ``pool.map`` calls per batch (the relations each
    definition reads, resolved through ``resolve``; then its lineage)."""
    extracted = pool.map([ExtractRequest(i.sql, i.dialect) for i in items])
    failures: dict[int, FailureReason] = {}
    pending: list[int] = []
    requests: list[LineageRequest] = []
    names: dict[int, tuple[RawName, ...]] = {}
    for n, (item, ex) in enumerate(zip(items, extracted, strict=True)):
        if isinstance(ex, LineageFailure):
            failures[n] = ex.reason
            continue
        assert isinstance(ex, tuple)
        pending.append(n)
        names[n] = tuple(ex)
        requests.append(LineageRequest(item.sql, item.dialect, resolve(ex, item.target), columns))
    parsed: dict[int, ParsedLineage] = {}
    for n, res in zip(pending, pool.map(requests), strict=True):
        if isinstance(res, ParsedLineage):
            parsed[n] = res
        else:
            failures[n] = res.reason if isinstance(res, LineageFailure) else "worker_died"
    out: list[DefinitionResult] = []
    for n, item in enumerate(items):
        p = parsed.get(n)
        out.append(
            DefinitionResult(
                target=item.target,
                dialect=item.dialect,
                sha256=definition_sha256(item.sql),
                parsed=p,
                failure=failures.get(n),
                template=None if p is None else template(item.sql, item.dialect),
                names=() if p is None else names.get(n, ()),
            )
        )
    return out


def _match_columns(parsed: Sequence[str], targets: Sequence[str]) -> list[str | None]:
    """The target column of each parsed column: exact first, then a unique case-insensitive
    match; a target column is claimed once."""
    out: list[str | None] = [c if c in targets else None for c in parsed]
    claimed = {c for c in out if c is not None}
    for n, col in enumerate(parsed):
        if out[n] is not None:
            continue
        cands = [t for t in targets if t.casefold() == col.casefold()]
        if len(cands) == 1 and cands[0] not in claimed:
            out[n] = cands[0]
            claimed.add(cands[0])
    return out


def lineage_facts(
    result: DefinitionResult,
    *,
    target_columns: Sequence[str],
    provenance: Provenance,
    source: str,
    observed_at: datetime | None,
    evidence: bool,
) -> list[Fact]:
    """The ``LINEAGE`` fact (when anything is upstream or unresolved) and one
    ``COLUMN_LINEAGE`` fact per parsed column that matches a target column. With
    ``evidence`` and a template, the template is attached to the ``LINEAGE`` fact only."""
    p = result.parsed
    if p is None:
        return []
    table = result.target.table_ref()
    facts: list[Fact] = []
    if p.upstream or p.unresolved:
        ev = (
            Evidence(sql=result.template, dialect=result.dialect)
            if evidence and result.template is not None
            else None
        )
        facts.append(
            Fact.make(
                FactKind.LINEAGE,
                table,
                table_lineage_value(p.upstream, p.unresolved),
                provenance,
                Status.UNVERIFIED,
                evidence=ev,
                source=source,
                observed_at=observed_at,
            )
        )
    matched = _match_columns([c.column for c in p.columns], target_columns)
    for col, target in zip(p.columns, matched, strict=True):
        if target is None:
            continue
        facts.append(
            Fact.make(
                FactKind.COLUMN_LINEAGE,
                ObjectRef(table.catalog, table.schema, table.table, target),
                column_lineage_value(col.inputs, col.transform, col.unresolved),
                provenance,
                Status.UNVERIFIED,
                source=source,
                observed_at=observed_at,
            )
        )
    return facts


def failure_diagnostic(target: ObjectRef, reason: FailureReason) -> Diagnostic:
    """SG211: a definition yielded no lineage; ``reason`` is a fixed category, never SQL."""
    return Diagnostic(
        "SG211",
        Severity.WARNING,
        f"lineage could not be parsed for {target.canonical()}: {reason}",
        location=target.canonical(),
    )


def extra_missing_diagnostic() -> Diagnostic:
    """SG212: emitted at most once per crawl or import."""
    return Diagnostic(
        "SG212",
        Severity.INFO,
        "column-level lineage needs the `lineage` extra (pip install 'schemaguru[lineage]')",
    )
