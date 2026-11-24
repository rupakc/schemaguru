"""The agent surface (spec 18): tool definitions, the dispatcher, the prompt block.

``schemaguru.agent.mcp`` (extra ``mcp``) serves the tools over stdio; ``schemaguru.agent.adapters``
maps them onto LangChain, LlamaIndex, OpenAI Agents and the Anthropic SDK (one extra each).
"""

from __future__ import annotations

from schemaguru.agent.dispatch import ToolError, handle_tool_call
from schemaguru.agent.prompt import prompt_block
from schemaguru.agent.tools import TOOL_NAMES, ToolHost, tool_definitions

__all__ = [
    "TOOL_NAMES",
    "ToolError",
    "ToolHost",
    "handle_tool_call",
    "prompt_block",
    "tool_definitions",
]
