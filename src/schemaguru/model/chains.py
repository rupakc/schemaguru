"""Identity chains / join recipes (spec 4.6)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from schemaguru.model.joins import JoinRef
from schemaguru.model.refs import ObjectRef


@dataclass(frozen=True)
class FilterStep:
    table: ObjectRef
    filter_text: str


@dataclass(frozen=True)
class NoteStep:
    text: str


Step = JoinRef | FilterStep | NoteStep


def _step_to_dict(s: Step) -> dict[str, Any]:
    if isinstance(s, JoinRef):
        return {"step": "join", **s.to_dict()}
    if isinstance(s, FilterStep):
        return {"step": "filter", "table": s.table.to_dict(), "filter_text": s.filter_text}
    return {"step": "note", "text": s.text}


def _step_from_dict(d: Mapping[str, Any]) -> Step:
    kind = d["step"]
    if kind == "join":
        return JoinRef.from_dict(d)
    if kind == "filter":
        return FilterStep(ObjectRef.from_dict(d["table"]), d["filter_text"])
    if kind == "note":
        return NoteStep(d["text"])
    raise ValueError(f"unknown chain step: {kind!r}")


@dataclass(frozen=True)
class Chain:
    name: str
    steps: tuple[Step, ...]
    because: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "steps": [_step_to_dict(s) for s in self.steps],
            "because": self.because,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Chain:
        return cls(d["name"], tuple(_step_from_dict(s) for s in d["steps"]), d["because"])
