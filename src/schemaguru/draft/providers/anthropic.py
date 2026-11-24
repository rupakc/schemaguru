"""``AnthropicProvider`` (extra ``anthropic``): structured output through a single tool.

The reply is the ``input`` of an ``emit_draft`` ``tool_use`` block whose ``input_schema`` is the
requested schema. Current models (incl. the default ``claude-sonnet-5-5``) reject a *forced*
``tool_choice`` (``{"type": "tool"}`` returns HTTP 400), so the default is ``{"type": "auto"}``
plus an instruction to answer only through the tool; a reply without the tool call is an
:class:`InvalidReply`, which the pipeline retries once. ``force_tool=True`` restores forcing for
models that accept it. ``usage.input_tokens`` / ``usage.output_tokens`` become ``last_usage``.
"""

from __future__ import annotations

import importlib
from typing import Any

from schemaguru.draft.provider import InvalidReply, Usage

TOOL_NAME = "emit_draft"
_INSTRUCTION = f"\n\nAnswer only by calling the `{TOOL_NAME}` tool exactly once."


def _get(obj: Any, key: str) -> Any:
    return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        model: str = "claude-sonnet-5-5",
        client: Any = None,
        max_retries: int = 1,
        *,
        async_client: Any = None,
        force_tool: bool = False,
    ) -> None:
        self.model = model
        self.max_retries = max_retries
        self.force_tool = force_tool
        self._client = client
        self._async_client = async_client
        self.last_usage: Usage | None = None

    def _sdk(self) -> Any:
        try:
            return importlib.import_module("anthropic")
        except ImportError as e:
            raise ImportError(
                "AnthropicProvider needs the 'anthropic' extra: pip install schemaguru[anthropic]"
            ) from e

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = self._sdk().Anthropic(max_retries=self.max_retries)
        return self._client

    @property
    def async_client(self) -> Any:
        if self._async_client is None:
            self._async_client = self._sdk().AsyncAnthropic(max_retries=self.max_retries)
        return self._async_client

    def _request(
        self, system: str, user: str, schema: dict[str, Any], max_tokens: int
    ) -> dict[str, Any]:
        tool_choice: dict[str, Any] = (
            {"type": "tool", "name": TOOL_NAME} if self.force_tool else {"type": "auto"}
        )
        return {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system + _INSTRUCTION,
            "messages": [{"role": "user", "content": user}],
            "tools": [
                {
                    "name": TOOL_NAME,
                    "description": "Emit the structured draft for the tables in the input.",
                    "input_schema": schema,
                }
            ],
            "tool_choice": tool_choice,
        }

    def _parse(self, resp: Any) -> dict[str, Any]:
        usage = _get(resp, "usage")
        if usage is not None:
            self.last_usage = Usage(
                int(_get(usage, "input_tokens") or 0), int(_get(usage, "output_tokens") or 0)
            )
        stop = _get(resp, "stop_reason")
        if stop in ("refusal", "max_tokens"):
            raise InvalidReply(f"the model stopped with stop_reason={stop!r}")
        for block in _get(resp, "content") or ():
            if _get(block, "type") == "tool_use" and _get(block, "name") == TOOL_NAME:
                data = _get(block, "input")
                if isinstance(data, dict):
                    return dict(data)
                raise InvalidReply("emit_draft input is not an object")
        raise InvalidReply("the reply has no emit_draft tool call")

    def complete_json(
        self, system: str, user: str, schema: dict[str, Any], *, max_tokens: int
    ) -> dict[str, Any]:
        self.last_usage = None
        resp = self.client.messages.create(**self._request(system, user, schema, max_tokens))
        return self._parse(resp)

    async def acomplete_json(
        self, system: str, user: str, schema: dict[str, Any], *, max_tokens: int
    ) -> dict[str, Any]:
        self.last_usage = None
        req = self._request(system, user, schema, max_tokens)
        resp = await self.async_client.messages.create(**req)
        return self._parse(resp)
