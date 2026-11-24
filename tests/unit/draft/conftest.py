"""Helpers for drafting tests: scripted providers and valid replies (no network, no cassettes)."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from schemaguru.draft.providers import CallableProvider


def table_reply(name: str, **over: Any) -> dict[str, Any]:
    """A schema-valid draft for one table; ``over`` replaces top-level fields."""
    d: dict[str, Any] = {
        "table": name,
        "description": f"One row per {name} record.",
        "columns": [],
        "use_cases": [f"How many {name} are there?"],
        "entity_type": None,
        "pitfalls": [],
        "soft_delete": None,
        "type_notes": [],
        "pii": [],
        "join_candidates": [],
        "glossary": [],
    }
    d.update(over)
    return d


class Script:
    """A provider fn that records every call and answers from the input's own table list."""

    def __init__(
        self, answer: Callable[[list[dict[str, Any]], int], dict[str, Any] | str] | None = None
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.answer = answer

    def __call__(self, system: str, user: str, schema: dict[str, Any], max_tokens: int) -> Any:
        payload = json.loads(user)
        self.calls.append({"system": system, "user": user, "payload": payload})
        if self.answer is not None:
            return self.answer(payload["tables"], len(self.calls))
        return {"tables": [table_reply(t["table"]) for t in payload["tables"]]}

    def provider(self) -> CallableProvider:
        return CallableProvider(self, name="script", model="m1")
