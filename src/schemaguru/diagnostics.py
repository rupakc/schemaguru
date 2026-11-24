"""Diagnostics (layer 0): findings reported instead of raised (spec 20).

Codes are ``SGnnn``: ``SG1xx`` connection/catalog, ``SG2xx`` overlays, ``SG3xx`` measurement
and budget, ``SG4xx`` drafting/LLM, ``SG5xx`` scoping/rendering, ``SG6xx`` graph, ``SG7xx``
store, ``SG8xx`` server/agent. ``CODES`` is the single registry: a task that emits a new code
adds it here (code -> one-line meaning); ``docs/reference/diagnostics.md`` must equal this set.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any


class Severity(Enum):
    ERROR = "ERROR"
    WARNING = "WARNING"
    INFO = "INFO"


CODES: Mapping[str, str] = MappingProxyType(
    {
        "SG101": "no catalog reader is registered for the requested dialect",
        "SG102": "the crawl skipped a catalog, schema, page or batch that failed to read",
        "SG104": "metadata SQL was refused: only a single SELECT/WITH/SHOW/DESCRIBE is allowed",
        "SG105": "the installed FactLang does not provide the metadata-SQL capability",
        "SG201": "overlay 'version' is missing or unsupported",
        "SG202": "overlay has an unknown key (a close match is suggested)",
        "SG203": "overlay reference does not resolve to anything in the store",
        "SG204": "overlay reference is ambiguous; the candidates are listed",
        "SG205": "overlay filter text is invalid",
        "SG206": "overlay waiver has no valid 'expires' date and is ignored",
        "SG207": "overlay waiver has expired and is ignored",
        "SG208": "overlay file is unreadable or an entry is malformed",
        "SG209": "dbt manifest is unreadable",
        "SG301": "probe over the per-probe byte budget was not run (fact ERROR over_budget)",
        "SG302": "total measurement budget exhausted; the remaining tables stay queued",
        "SG303": "a measurement probe failed (fact ERROR for that subject only)",
        "SG304": "the installed FactLang does not provide a needed measurement capability",
        "SG305": "a table asked to be measured is not in the store (crawl it first)",
        "SG401": "an LLM draft reply was invalid twice (schema or unknown names); fact ERROR",
        "SG402": "draft token budget exhausted; the remaining tables were not drafted",
        "SG403": "the LLM provider call failed; that call's tables get an ERROR draft fact",
        "SG404": "a table asked to be drafted is not in the store (crawl it first)",
        "SG405": "a drafted claim could not be checked (no verdict); the draft stays UNVERIFIED",
        "SG601": "a malformed join fact was skipped; only that graph edge is lost",
        "SG602": "a waypoint rule is malformed; the searches it governs are refused",
        "SG603": "a waypoint rule has more than 16 requirements; its searches are refused",
        "SG604": "a waypoint prohibition matches no edge in the graph",
        "SG701": "SQLite FTS5 is unavailable; search falls back to a slower LIKE scan",
        "SG702": "the store was written by a newer schema version than this library supports",
        "SG801": "guard refused the SQL: not a single read-only SELECT/WITH statement",
        "SG802": "guard refused the SQL: it reads a never-query table",
    }
)


@dataclass(frozen=True)
class Diagnostic:
    code: str
    severity: Severity
    message: str
    location: str | None = None
    hint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
            "location": self.location,
            "hint": self.hint,
        }


class DiagnosticBag:
    """An ordered collection of diagnostics."""

    def __init__(self) -> None:
        self._items: list[Diagnostic] = []

    def add(self, diagnostic: Diagnostic) -> None:
        self._items.append(diagnostic)

    def extend(self, diagnostics: Iterable[Diagnostic]) -> None:
        self._items.extend(diagnostics)

    def errors(self) -> list[Diagnostic]:
        return [d for d in self._items if d.severity is Severity.ERROR]

    def __iter__(self) -> Iterator[Diagnostic]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def to_list(self) -> list[dict[str, Any]]:
        return [d.to_dict() for d in self._items]
