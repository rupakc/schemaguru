"""Evidence attached to a fact: the outcome of an executed FactLang primitive (spec 4.3)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Evidence:
    claim_text: str | None = None
    verdict: str | None = None
    sql: str | None = None
    params: tuple[Any, ...] = ()
    dialect: str | None = None
    tier: str | None = None
    duration_ms: float | None = None
    bytes_processed: int | None = None
    sample: Mapping[str, Any] | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_text": self.claim_text,
            "verdict": self.verdict,
            "sql": self.sql,
            "params": list(self.params),
            "dialect": self.dialect,
            "tier": self.tier,
            "duration_ms": self.duration_ms,
            "bytes_processed": self.bytes_processed,
            "sample": None if self.sample is None else dict(self.sample),
            "raw": dict(self.raw),
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Evidence:
        return cls(
            claim_text=d.get("claim_text"),
            verdict=d.get("verdict"),
            sql=d.get("sql"),
            params=tuple(d.get("params") or ()),
            dialect=d.get("dialect"),
            tier=d.get("tier"),
            duration_ms=d.get("duration_ms"),
            bytes_processed=d.get("bytes_processed"),
            sample=d.get("sample"),
            raw=dict(d.get("raw") or {}),
        )
