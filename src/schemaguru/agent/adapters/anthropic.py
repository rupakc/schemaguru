"""Anthropic SDK adapter (spec 18.4): tool definitions, a ``tool_use`` runner, and
``BetaFunctionTool`` objects for the SDK's tool runner (``client.beta.messages.tool_runner``)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from schemaguru.agent.adapters import need
from schemaguru.agent.dispatch import handle_tool_call
from schemaguru.agent.tools import ToolHost, tool_definitions


def tools(guru: ToolHost) -> list[dict[str, Any]]:
    """``tools=`` for ``messages.create``: ``{"name", "description", "input_schema"}`` each."""
    return tool_definitions(guru)


def _field(block: Any, key: str) -> Any:
    return block.get(key) if isinstance(block, Mapping) else getattr(block, key, None)


def run_tool_use(guru: ToolHost, block: Any) -> dict[str, Any]:
    """Answer one ``tool_use`` block (an SDK object or a dict) with a ``tool_result`` block."""
    args = _field(block, "input")
    result = handle_tool_call(guru, str(_field(block, "name")), {} if args is None else args)
    return {
        "type": "tool_result",
        "tool_use_id": str(_field(block, "id")),
        "content": result["text"],
        "is_error": "error" in result,
    }


def runner_tools(guru: ToolHost) -> list[Any]:
    """``BetaFunctionTool`` objects for ``client.beta.messages.tool_runner(tools=...)``."""
    lib = need("anthropic.lib.tools", "anthropic")
    out: list[Any] = []
    for spec in tool_definitions(guru):

        def run(_name: str = spec["name"], **kwargs: Any) -> str:
            return str(handle_tool_call(guru, _name, kwargs)["text"])

        out.append(
            lib.BetaFunctionTool(
                run,
                name=spec["name"],
                description=spec["description"],
                input_schema=spec["input_schema"],
            )
        )
    return out


__all__ = ["run_tool_use", "runner_tools", "tools"]
