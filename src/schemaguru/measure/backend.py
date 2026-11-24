"""The measurement backend protocol: the only way SchemaGuru gets numbers from a warehouse.

The real backend is :class:`schemaguru.measure.flbridge.FactLangBackend`; tests use fakes.

**Payload contract** (what ``MeasureResult.payload`` holds per request; FactLang wire-format
aliases, numbers as ``int``/``float``/``Decimal``; a missing key means unknown). For a sampled
request the numbers are the *raw metrics on the sampled rows* (FactLang Amendment B3) plus
``sampled_rows`` (``sampled_left_rows``/``sampled_right_rows`` for a join); the host scales.

* ``ProfileColumns``: ``row_count`` and ``columns: {name: {non_null, distinct_keys}}``.
* ``TopValues``: ``non_null``, ``distinct`` (distinct non-null values), ``values: [{value,
  count}]`` ranked by count desc, value asc.
* ``KeyOverlapReq``: ``distinct_left, matched, distinct_right, left_non_null,
  matched_left_rows`` and, with multiplicity, ``max_left_per_key, max_right_per_key``.
* ``Uniqueness``: ``row_count, non_null, distinct_keys, max_rows_per_key, duplicate_keys``.
* ``Freshness``: ``max_value`` and, when the backend knows it, ``server_now``.
* ``PatternRate``: ``non_null, matching``.
* ``RowCount``: ``row_count``.

``payload=None`` with no error means the probe returned no rows (FactLang NO_DATA).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from schemaguru.diagnostics import Diagnostic
from schemaguru.measure.requests import Request
from schemaguru.model.evidence import Evidence


@dataclass(frozen=True)
class MeasureResult:
    request: Request
    payload: Mapping[str, Any] | None
    evidence: Evidence | None
    error: Diagnostic | None


@dataclass(frozen=True)
class CostEstimate:
    """``bytes=None`` is unknown, never a guess; ``heuristic``: SchemaGuru's catalog bytes."""

    request: Request
    bytes: int | None
    method: Literal["dry_run", "heuristic", "unknown"]


class MeasureBackend(Protocol):
    def measure(self, reqs: Sequence[Request]) -> list[MeasureResult]:
        """One result per request, in order. Never raises for execution failures."""
        ...

    def estimate(self, reqs: Sequence[Request]) -> list[CostEstimate]:
        """Price without executing; one estimate per request, in order."""
        ...

    def check(self, claims: str) -> list[Evidence]:
        """Verify FactLang claim text (never sampled); one ``Evidence`` per outcome."""
        ...

    def capabilities(self) -> frozenset[str]: ...
