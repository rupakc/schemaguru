"""Providers against fake SDK clients shaped like the real responses (no network, no cassettes)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace as NS
from typing import Any

import pytest

from schemaguru.draft.provider import DRAFT_SCHEMA, InvalidReply, LLMProvider, Usage
from schemaguru.draft.providers import (
    AnthropicProvider,
    CallableProvider,
    OpenAICompatibleProvider,
)

REPLY = {"tables": []}


class FakeMessages:
    def __init__(self, resp: Any) -> None:
        self.resp = resp
        self.kwargs: dict[str, Any] = {}

    def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        return self.resp


class AsyncFakeMessages(FakeMessages):
    async def create(self, **kwargs: Any) -> Any:  # type: ignore[override]
        self.kwargs = kwargs
        return self.resp


def _anthropic_resp(content: list[Any], stop: str = "tool_use") -> Any:
    return NS(content=content, stop_reason=stop, usage=NS(input_tokens=321, output_tokens=45))


def test_anthropic_parses_tool_use_block() -> None:
    resp = _anthropic_resp(
        [NS(type="text", text="ok"), NS(type="tool_use", name="emit_draft", input=REPLY)]
    )
    msgs = FakeMessages(resp)
    p = AnthropicProvider(client=NS(messages=msgs))
    assert isinstance(p, LLMProvider)
    assert p.complete_json("sys", "usr", DRAFT_SCHEMA, max_tokens=2000) == REPLY
    assert p.last_usage == Usage(321, 45)
    k = msgs.kwargs
    assert k["model"] == "claude-sonnet-5-5" and k["max_tokens"] == 2000
    assert k["tools"] == [
        {
            "name": "emit_draft",
            "description": k["tools"][0]["description"],
            "input_schema": DRAFT_SCHEMA,
        }
    ]
    assert k["tool_choice"] == {"type": "auto"}
    assert "emit_draft" in k["system"] and k["messages"] == [{"role": "user", "content": "usr"}]


def test_anthropic_force_tool_and_model() -> None:
    msgs = FakeMessages(_anthropic_resp([NS(type="tool_use", name="emit_draft", input=REPLY)]))
    p = AnthropicProvider(model="m-x", client=NS(messages=msgs), force_tool=True)
    p.complete_json("s", "u", DRAFT_SCHEMA, max_tokens=10)
    assert msgs.kwargs["tool_choice"] == {"type": "tool", "name": "emit_draft"}
    assert msgs.kwargs["model"] == "m-x"


@pytest.mark.parametrize(
    "resp",
    [
        _anthropic_resp([NS(type="text", text="I think...")], stop="end_turn"),
        _anthropic_resp([], stop="refusal"),
        _anthropic_resp([NS(type="tool_use", name="emit_draft", input=REPLY)], stop="max_tokens"),
        _anthropic_resp([NS(type="tool_use", name="other", input=REPLY)]),
        _anthropic_resp([NS(type="tool_use", name="emit_draft", input="[]")]),
    ],
)
def test_anthropic_invalid_replies(resp: Any) -> None:
    p = AnthropicProvider(client=NS(messages=FakeMessages(resp)))
    with pytest.raises(InvalidReply):
        p.complete_json("s", "u", DRAFT_SCHEMA, max_tokens=10)
    assert p.last_usage == Usage(321, 45)  # spent tokens are reported even on failure


def test_anthropic_async_and_dict_blocks() -> None:
    resp = {
        "content": [{"type": "tool_use", "name": "emit_draft", "input": REPLY}],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 1, "output_tokens": 2},
    }
    p = AnthropicProvider(async_client=NS(messages=AsyncFakeMessages(resp)))
    assert asyncio.run(p.acomplete_json("s", "u", DRAFT_SCHEMA, max_tokens=10)) == REPLY
    assert p.last_usage == Usage(1, 2)


def _openai_resp(content: Any, refusal: Any = None) -> Any:
    return NS(
        choices=[NS(message=NS(content=content, refusal=refusal))],
        usage=NS(prompt_tokens=11, completion_tokens=7),
    )


def test_openai_compatible_json() -> None:
    comp = FakeMessages(_openai_resp(json.dumps(REPLY)))
    p = OpenAICompatibleProvider(
        "http://localhost:8000/v1", "k", "local-model", client=NS(chat=NS(completions=comp))
    )
    assert p.complete_json("sys", "usr", DRAFT_SCHEMA, max_tokens=99) == REPLY
    assert p.last_usage == Usage(11, 7)
    k = comp.kwargs
    assert k["model"] == "local-model" and k["max_tokens"] == 99
    assert k["response_format"]["type"] == "json_schema"
    assert k["response_format"]["json_schema"]["schema"] == DRAFT_SCHEMA
    assert k["messages"][0] == {"role": "system", "content": "sys"}


@pytest.mark.parametrize(
    "resp",
    [
        _openai_resp("not json"),
        _openai_resp("[1]"),
        _openai_resp(None),
        _openai_resp(None, refusal="no"),
        NS(choices=[], usage=None),
    ],
)
def test_openai_invalid(resp: Any) -> None:
    p = OpenAICompatibleProvider(
        None, None, "m", client=NS(chat=NS(completions=FakeMessages(resp)))
    )
    with pytest.raises(InvalidReply):
        p.complete_json("s", "u", DRAFT_SCHEMA, max_tokens=1)


def test_openai_async() -> None:
    comp = AsyncFakeMessages(_openai_resp(json.dumps(REPLY)))
    p = OpenAICompatibleProvider(None, None, "m", async_client=NS(chat=NS(completions=comp)))
    assert asyncio.run(p.acomplete_json("s", "u", DRAFT_SCHEMA, max_tokens=1)) == REPLY


def test_callable_provider_forms() -> None:
    p = CallableProvider(lambda s, u, sc, mt: json.dumps(REPLY))
    assert p.complete_json("s", "u", DRAFT_SCHEMA, max_tokens=1) == REPLY and p.last_usage is None
    p = CallableProvider(lambda s, u, sc, mt: (REPLY, Usage(3, 4)))
    assert p.complete_json("s", "u", DRAFT_SCHEMA, max_tokens=1) == REPLY
    assert p.last_usage == Usage(3, 4)
    with pytest.raises(InvalidReply):
        CallableProvider(lambda *a: "[]").complete_json("s", "u", DRAFT_SCHEMA, max_tokens=1)
    with pytest.raises(InvalidReply):
        CallableProvider(lambda *a: "{").complete_json("s", "u", DRAFT_SCHEMA, max_tokens=1)

    async def afn(*a: Any) -> Any:
        return REPLY

    ap = CallableProvider(afn)
    assert asyncio.run(ap.acomplete_json("s", "u", DRAFT_SCHEMA, max_tokens=1)) == REPLY
    with pytest.raises(TypeError):
        ap.complete_json("s", "u", DRAFT_SCHEMA, max_tokens=1)
    sp = CallableProvider(lambda *a: REPLY)
    assert asyncio.run(sp.acomplete_json("s", "u", DRAFT_SCHEMA, max_tokens=1)) == REPLY


def test_sdk_extras_missing_raise_helpful_import_error() -> None:
    import importlib.util

    if importlib.util.find_spec("anthropic") is None:
        with pytest.raises(ImportError, match="anthropic"):
            _ = AnthropicProvider().client
    if importlib.util.find_spec("openai") is None:
        with pytest.raises(ImportError, match="openai"):
            _ = OpenAICompatibleProvider(None, None, "m").client
