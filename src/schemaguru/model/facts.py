"""Facts: the unit of intelligence (spec 4.3, 4.4)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from fnmatch import fnmatchcase
from typing import Any

from schemaguru._util.hashing import stable_hash
from schemaguru._util.timeutil import require_aware
from schemaguru.model.evidence import Evidence
from schemaguru.model.joins import JoinRef
from schemaguru.model.kinds import KindSpec, get_kind
from schemaguru.model.refs import ObjectRef


class Provenance(Enum):
    CATALOG = "CATALOG"
    DECLARED = "DECLARED"
    MEASURED = "MEASURED"
    DRAFTED = "DRAFTED"
    HUMAN = "HUMAN"


class Status(Enum):
    OBSERVED = "OBSERVED"
    VERIFIED = "VERIFIED"
    CONTRADICTED = "CONTRADICTED"
    UNVERIFIED = "UNVERIFIED"
    ESTIMATED = "ESTIMATED"
    UNMEASURABLE = "UNMEASURABLE"
    ERROR = "ERROR"
    STALE = "STALE"
    WAIVED = "WAIVED"


def _value_identity(kind: KindSpec, value: Mapping[str, Any]) -> dict[str, Any]:
    """The payload part of a fact's id. A missing identity field is a programmer error.

    Present-but-``None`` is a known unknown and is fine; an absent key would silently merge
    facts that differ in that field (e.g. two FRESHNESS columns), so it raises.
    """
    if kind.identity_fields is None:
        return dict(value)
    missing = [f for f in kind.identity_fields if f not in value]
    if missing:
        raise ValueError(f"{kind.name} value lacks identity field(s): {', '.join(missing)}")
    return {f: value[f] for f in kind.identity_fields}


def method_class(method: str | None, status: Status | None = None) -> str:
    """``"error"`` for a failed probe, ``"sampled"`` for a ``sampled:...`` method, else
    ``"exact"`` (spec 8.5, 8.6). Part of a MEASURED fact's id, so a failure never replaces the
    last good measurement and a sample never replaces an exact one."""
    if status is Status.ERROR:
        return "error"
    return "sampled" if (method or "").startswith("sampled:") else "exact"


@dataclass(frozen=True)
class Fact:
    id: str
    kind: KindSpec
    subject: ObjectRef | JoinRef
    value: Mapping[str, Any]
    provenance: Provenance
    status: Status
    method: str | None = None
    evidence: Evidence | None = None
    source: str | None = None
    observed_at: datetime | None = None
    snapshot_id: str | None = None

    def __post_init__(self) -> None:
        if self.observed_at is not None:
            require_aware(self.observed_at)
        if self.provenance is Provenance.MEASURED and self.method is None:
            raise ValueError("a MEASURED fact requires a method")
        if self.status is Status.ESTIMATED and not (self.method or "").startswith("sampled:"):
            raise ValueError("an ESTIMATED fact requires a 'sampled:...' method")
        if self.status is Status.VERIFIED and (self.method or "").startswith("sampled:"):
            raise ValueError("a sampled number is never VERIFIED")
        if self.status is Status.VERIFIED and not (
            self.method == "exact"
            or (
                self.provenance in (Provenance.HUMAN, Provenance.DECLARED)
                and self.evidence is not None
            )
        ):
            raise ValueError(
                "VERIFIED requires method 'exact', or HUMAN/DECLARED provenance with evidence"
            )
        is_join = isinstance(self.subject, JoinRef)
        if (self.kind.subject == "join") != is_join:
            raise ValueError(f"{self.kind.name} subject must be {self.kind.subject}")
        if self.kind.subject == "column" and (
            not isinstance(self.subject, ObjectRef) or self.subject.column is None
        ):
            raise ValueError(f"{self.kind.name} subject must be a column ref")

    @classmethod
    def make(
        cls,
        kind: KindSpec | str,
        subject: ObjectRef | JoinRef,
        value: Mapping[str, Any],
        provenance: Provenance,
        status: Status,
        *,
        method: str | None = None,
        evidence: Evidence | None = None,
        source: str | None = None,
        observed_at: datetime | None = None,
        snapshot_id: str | None = None,
    ) -> Fact:
        """Build a fact; the id is always computed (never supplied)."""
        spec = get_kind(kind) if isinstance(kind, str) else kind
        subject_key = (
            subject.identity_key() if isinstance(subject, JoinRef) else subject.canonical()
        )
        ident: list[Any] = [spec.name, subject_key, provenance.value, _value_identity(spec, value)]
        if provenance is Provenance.MEASURED:
            # Exact and sampled measurements of one subject coexist: a newer sample must never
            # overwrite an older exact number in the store (spec 8.5: never compare methods).
            ident.append(method_class(method, status))
        fid = stable_hash(ident)
        return cls(
            fid, spec, subject, value, provenance, status, method, evidence, source,
            observed_at, snapshot_id,
        )  # fmt: skip

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.name,
            "subject": self.subject.to_dict(),
            "value": dict(self.value),
            "provenance": self.provenance.value,
            "status": self.status.value,
            "method": self.method,
            "evidence": None if self.evidence is None else self.evidence.to_dict(),
            "source": self.source,
            "observed_at": (
                None
                if self.observed_at is None
                else self.observed_at.astimezone(timezone.utc).isoformat()
            ),
            "snapshot_id": self.snapshot_id,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Fact:
        subj = d["subject"]
        subject: ObjectRef | JoinRef = (
            JoinRef.from_dict(subj) if "left" in subj else ObjectRef.from_dict(subj)
        )
        at = d.get("observed_at")
        ev = d.get("evidence")
        return cls(
            id=d["id"],
            kind=get_kind(d["kind"]),
            subject=subject,
            value=dict(d["value"]),
            provenance=Provenance(d["provenance"]),
            status=Status(d["status"]),
            method=d.get("method"),
            evidence=None if ev is None else Evidence.from_dict(ev),
            source=d.get("source"),
            observed_at=None if at is None else datetime.fromisoformat(at),
            snapshot_id=d.get("snapshot_id"),
        )


@dataclass(frozen=True)
class Waiver:
    """A human acknowledgement of a contradiction, with a mandatory expiry (spec 4.4).

    ``fact_pattern`` is ``"<KIND> <subject>"``: the kind name, one space, then the subject's
    canonical form (``ObjectRef.canonical()``, or ``JoinRef.identity_key()`` for joins). The kind
    part matches exactly; the subject part is a case-sensitive ``fnmatch`` glob, so
    ``"VALUES shop.orders.status"`` is exact and ``"VALUES shop.orders.*"`` covers every column.
    A waiver is active through its ``expires`` date (UTC) inclusive.
    """

    fact_pattern: str
    reason: str
    expires: date

    def __post_init__(self) -> None:
        kind, sep, subject = self.fact_pattern.partition(" ")
        if not (kind and sep and subject.strip()):
            raise ValueError(f"waiver pattern must be '<KIND> <subject>': {self.fact_pattern!r}")
        if isinstance(self.expires, datetime) or not isinstance(self.expires, date):
            raise ValueError("a waiver requires an 'expires' date")

    def matches(self, kind_name: str, subject_key: str) -> bool:
        kind, _, subject = self.fact_pattern.partition(" ")
        return kind == kind_name and fnmatchcase(subject_key, subject)

    def active(self, now: datetime) -> bool:
        return now.astimezone(timezone.utc).date() <= self.expires
