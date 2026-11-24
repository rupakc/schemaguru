"""OpenAI Agents SDK adapter (spec 18.4, extra ``openai-agents``): ``FunctionTool``s."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

from schemaguru.agent.adapters import call_json, need
from schemaguru.agent.tools import ToolHost, tool_definitions


def _invoker(guru: ToolHost, name: str) -> Callable[[Any, str], Awaitable[str]]:
    """``on_invoke_tool`` for tool ``name`` only (closed over, never taken from the input)."""

    async def invoke(ctx: Any, raw: str) -> str:
        try:
            args: Any = json.loads(raw) if raw.strip() else {}
        except ValueError:
            args = "invalid JSON arguments"  # the dispatcher refuses a non-object (SG803)
        return await asyncio.to_thread(call_json, guru, name, args)

    return invoke


def function_tools(guru: ToolHost) -> list[Any]:
    """One ``FunctionTool`` per agent tool (non-strict schema: some arguments are optional);
    the tool output is the result as JSON text."""
    agents = need("agents", "openai-agents")
    out: list[Any] = []
    for spec in tool_definitions(guru):
        invoke = _invoker(guru, spec["name"])

        out.append(
            agents.FunctionTool(
                name=spec["name"],
                description=spec["description"],
                params_json_schema=spec["input_schema"],
                on_invoke_tool=invoke,
                strict_json_schema=False,
            )
        )
    return out


__all__ = ["function_tools"]
