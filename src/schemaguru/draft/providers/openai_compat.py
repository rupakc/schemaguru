"""``OpenAICompatibleProvider`` (extra ``openai``): any OpenAI-compatible chat endpoint.

Structured output via ``response_format={"type": "json_schema", ...}``; the reply is the JSON in
``choices[0].message.content``. ``usage.prompt_tokens`` / ``usage.completion_tokens`` become
``last_usage``. Works with local servers that speak the same API (pass their ``base_url``).
"""

from __future__ import annotations

import importlib
import json
from typing import Any

from schemaguru.draft.provider import InvalidReply, Usage


def _get(obj: Any, key: str) -> Any:
    return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)


class OpenAICompatibleProvider:
    name = "openai"

    def __init__(
        self,
        base_url: str | None,
        api_key: str | None,
        model: str,
        *,
        client: Any = None,
        async_client: Any = None,
        name: str = "openai",
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.name = name
        self._client = client
        self._async_client = async_client
        self.last_usage: Usage | None = None

    def _sdk(self) -> Any:
        try:
            return importlib.import_module("openai")
        except ImportError as e:
            raise ImportError(
                "OpenAICompatibleProvider needs the 'openai' extra: pip install schemaguru[openai]"
            ) from e

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = self._sdk().OpenAI(base_url=self.base_url, api_key=self.api_key)
        return self._client

    @property
    def async_client(self) -> Any:
        if self._async_client is None:
            self._async_client = self._sdk().AsyncOpenAI(
                base_url=self.base_url, api_key=self.api_key
            )
        return self._async_client

    def _request(
        self, system: str, user: str, schema: dict[str, Any], max_tokens: int
    ) -> dict[str, Any]:
        return {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "emit_draft", "schema": schema},
            },
        }

    def _parse(self, resp: Any) -> dict[str, Any]:
        usage = _get(resp, "usage")
        if usage is not None:
            self.last_usage = Usage(
                int(_get(usage, "prompt_tokens") or 0), int(_get(usage, "completion_tokens") or 0)
            )
        choices = _get(resp, "choices") or []
        if not choices:
            raise InvalidReply("the reply has no choices")
        msg = _get(choices[0], "message")
        refusal = _get(msg, "refusal")
        if refusal:
            raise InvalidReply(f"the model refused: {refusal}")
        content = _get(msg, "content")
        if not isinstance(content, str):
            raise InvalidReply("the reply has no text content")
        try:
            data = json.loads(content)
        except json.JSONDecodeError as e:
            raise InvalidReply(f"reply is not JSON: {e}") from None
        if not isinstance(data, dict):
            raise InvalidReply("reply is not a JSON object")
        return data

    def complete_json(
        self, system: str, user: str, schema: dict[str, Any], *, max_tokens: int
    ) -> dict[str, Any]:
        self.last_usage = None
        resp = self.client.chat.completions.create(
            **self._request(system, user, schema, max_tokens)
        )
        return self._parse(resp)

    async def acomplete_json(
        self, system: str, user: str, schema: dict[str, Any], *, max_tokens: int
    ) -> dict[str, Any]:
        self.last_usage = None
        req = self._request(system, user, schema, max_tokens)
        resp = await self.async_client.chat.completions.create(**req)
        return self._parse(resp)
