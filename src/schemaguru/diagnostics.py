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


CODES: Mapping[str, str] = MappingProxyType({})


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
