"""Facts: the unit of intelligence (spec 4.3, 4.4)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
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
    if kind.identity_fields is None:
        return dict(value)
    return {f: value.get(f) for f in kind.identity_fields}


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
        fid = stable_hash(
            [spec.name, subject.canonical(), provenance.value, _value_identity(spec, value)]
        )
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
