"""``CallableProvider``: wrap any function as an :class:`LLMProvider` (tests, custom backends)."""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable
from typing import Any

from schemaguru.draft.provider import InvalidReply, Usage


def _as_reply(raw: Any) -> tuple[dict[str, Any], Usage | None]:
    usage: Usage | None = None
    if isinstance(raw, tuple) and len(raw) == 2 and isinstance(raw[1], Usage | type(None)):
        raw, usage = raw
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as e:
            raise InvalidReply(f"reply is not JSON: {e}") from None
    if not isinstance(raw, dict):
        raise InvalidReply(f"reply is not a JSON object: {type(raw).__name__}")
    return raw, usage


class CallableProvider:
    """``fn(system, user, schema, max_tokens)`` returns a dict, a JSON string, or
    ``(reply, Usage)`` when it knows the token usage. ``fn`` may be ``async``.

    Without a reported ``Usage`` the drafting pipeline estimates tokens from text length.
    """

    def __init__(
        self, fn: Callable[..., Any], *, name: str = "callable", model: str = "custom"
    ) -> None:
        self.fn = fn
        self.name = name
        self.model = model
        self.last_usage: Usage | None = None

    def complete_json(
        self, system: str, user: str, schema: dict[str, Any], *, max_tokens: int
    ) -> dict[str, Any]:
        self.last_usage = None
        raw = self.fn(system, user, schema, max_tokens)
        if inspect.isawaitable(raw):
            close = getattr(raw, "close", None)
            if callable(close):
                close()  # never leave an un-awaited coroutine behind
            raise TypeError("an async fn needs acomplete_json")
        reply, self.last_usage = _as_reply(raw)
        return reply

    async def acomplete_json(
        self, system: str, user: str, schema: dict[str, Any], *, max_tokens: int
    ) -> dict[str, Any]:
        self.last_usage = None
        raw = self.fn(system, user, schema, max_tokens)
        if inspect.isawaitable(raw):
            raw = await raw
        reply, self.last_usage = _as_reply(raw)
        return reply
