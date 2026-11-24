"""OpenAI Agents SDK adapter (spec 18.4, extra ``openai-agents``): ``FunctionTool``s."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from schemaguru.agent.adapters import call_json, need
from schemaguru.agent.tools import ToolHost, tool_definitions


def function_tools(guru: ToolHost) -> list[Any]:
    """One ``FunctionTool`` per agent tool (non-strict schema: some arguments are optional);
    the tool output is the result as JSON text."""
    agents = need("agents", "openai-agents")
    out: list[Any] = []
    for spec in tool_definitions(guru):

        async def invoke(ctx: Any, raw: str, _name: str = spec["name"]) -> str:
            try:
                args = json.loads(raw) if raw.strip() else {}
            except ValueError:
                args = "invalid JSON arguments"  # the dispatcher refuses a non-object (SG803)
            return await asyncio.to_thread(call_json, guru, _name, args)

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
