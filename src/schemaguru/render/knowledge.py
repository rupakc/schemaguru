"""Everything resolved for one table: the input of every profile (spec 13).

``collect`` reads the store by index only (the table's own and its columns' facts) plus the join
facts touching the table, and resolves them once with the precedence rules of
``knowledge.resolve``. The result depends only on the stored content, ``now`` and the config,
never on insertion order (spec 12.2).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime

from schemaguru._util.timeutil import require_aware
from schemaguru.config import Config
from schemaguru.knowledge.resolve import Effective, resolve
from schemaguru.knowledge.soft_delete import MISMATCH, fact_mismatch
from schemaguru.model.facts import Fact, Provenance, Status, Waiver
from schemaguru.model.joins import JoinRef
from schemaguru.model.kinds import LINEAGE_KINDS
from schemaguru.model.lineage import LineageResolution, resolve_lineage
from schemaguru.model.refs import ObjectRef
from schemaguru.model.structure import Column, Constraint, Table
from schemaguru.store.protocol import Store


@dataclass(frozen=True)
class TableKnowledge:
    """One table's structure plus its effective knowledge.

    ``effective`` is keyed like :func:`resolve` (``(kind name, subject key)``) and covers the
    table, its columns and every join with the table on either side. ``facts`` are the raw facts
    (for the LITE column priority). ``config`` supplies thresholds and caps at render time.
    """

    ref: ObjectRef
    table: Table | None
    columns: tuple[Column, ...]
    constraints: tuple[Constraint, ...]
    effective: Mapping[tuple[str, str], Effective]
    facts: tuple[Fact, ...]
    now: datetime
    config: Config
    # human-wins lineage of the table and its columns (never part of ``effective``)
    lineage: LineageResolution = field(default_factory=lambda: LineageResolution((), (), ()))

    def get(self, kind: str, column: str | None = None) -> Effective | None:
        subject = self.ref if column is None else self.column_ref(column)
        return self.effective.get((kind, subject.canonical()))

    def column_ref(self, name: str) -> ObjectRef:
        r = self.ref
        return ObjectRef(r.catalog, r.schema, r.table, name)

    def joins(self) -> list[Effective]:
        """Effective joins touching the table, by canonical join identity."""
        return [self.effective[k] for k in sorted(self.effective) if k[0] == "JOIN"]

    def measured(self) -> bool:
        """Whether any measurement (even a failed one) exists for the table or its columns."""
        for f in self.facts:
            if f.provenance is not Provenance.MEASURED:
                continue
            subj = f.subject
            if isinstance(subj, JoinRef):
                if subj.left[0].table_ref() == self.ref:
                    return True
            elif subj.table_ref() == self.ref:
                return True
        return False


def _touches(join: JoinRef, ref: ObjectRef) -> bool:
    return join.left[0].table_ref() == ref or join.right[0].table_ref() == ref


def collect(
    store: Store,
    ref: ObjectRef,
    now: datetime,
    cfg: Config,
    *,
    join_facts: Sequence[Fact] | None = None,
    waivers: Sequence[Waiver] = (),
) -> TableKnowledge:
    """Resolve everything known about ``ref`` at ``now``.

    ``join_facts`` lets a caller rendering many tables load the JOIN facts once; by default they
    are read from the store (by kind, an indexed read).
    """
    require_aware(now)
    ref = ref.table_ref()
    columns = tuple(sorted(store.columns(ref), key=lambda c: (c.ordinal, c.ref.column or "")))
    constraints = tuple(
        sorted(
            store.constraints(ref),
            key=lambda c: (
                c.kind,
                c.columns,
                "" if c.ref_table is None else c.ref_table.canonical(),
                c.ref_columns or (),
            ),
        )
    )
    subjects: list[ObjectRef | JoinRef] = [ref, *(c.ref for c in columns)]
    own = store.facts_for(subjects, None)
    joins = store.facts_for(None, ["JOIN"]) if join_facts is None else join_facts
    touching = [f for f in joins if isinstance(f.subject, JoinRef) and _touches(f.subject, ref)]
    facts = sorted({f.id: f for f in (*own, *touching)}.values(), key=lambda f: f.id)
    plain = [f for f in facts if f.kind.name not in LINEAGE_KINDS]
    lineage = resolve_lineage(f for f in facts if f.kind.name in LINEAGE_KINDS)
    effective = resolve(plain, now=now, ttl_days=cfg.fact_ttl_days, waivers=waivers)
    effective = _check_soft_delete(effective, ref, columns, facts, now, waivers)
    return TableKnowledge(
        ref=ref,
        table=store.table(ref),
        columns=columns,
        constraints=constraints,
        effective=effective,
        facts=tuple(facts),
        now=now,
        config=cfg,
        lineage=lineage,
    )


def _check_soft_delete(
    effective: dict[tuple[str, str], Effective],
    ref: ObjectRef,
    columns: Sequence[Column],
    facts: Sequence[Fact],
    now: datetime,
    waivers: Sequence[Waiver],
) -> dict[tuple[str, str], Effective]:
    """A human/declared live-rows filter that does not fit the catalog or the measured values
    becomes CONTRADICTED (WAIVED under a live waiver) with the reason, so the view warns and the
    snippet leaves it out (spec 15: an expectation is checked, never applied silently)."""
    key = ("SOFT_DELETE", ref.canonical())
    eff = effective.get(key)
    p = None if eff is None else eff.primary
    if eff is None or p is None or p.status not in (Status.UNVERIFIED, Status.OBSERVED):
        return effective
    why = fact_mismatch(p, columns, facts)
    if why is None:
        return effective
    msg = MISMATCH + why
    waiver = next((w for w in waivers if w.active(now) and w.matches(*key)), None)
    if waiver is not None:
        msg = f"WAIVED until {waiver.expires.isoformat()} ({waiver.reason}): {msg}"
    checked = replace(p, status=Status.CONTRADICTED if waiver is None else Status.WAIVED)
    exp = eff.expectation
    out = dict(effective)
    out[key] = replace(
        eff,
        primary=checked,
        expectation=checked if exp is not None and exp.id == p.id else exp,
        contradictions=(*eff.contradictions, msg),
        all=tuple(checked if f.id == p.id else f for f in eff.all),
    )
    return out
