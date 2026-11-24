"""Framework adapters (spec 18.4): each maps :func:`schemaguru.agent.tool_definitions` onto one
framework's tool type and calls :func:`schemaguru.agent.handle_tool_call`; no other logic.

One module per framework, each needing its own extra and importing it lazily:
``langchain`` (``langchain-core``), ``llamaindex`` (``llama-index-core``), ``openai_agents``
(``openai-agents``), ``anthropic`` (``anthropic``; the plain tool dicts need nothing).
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Callable
from typing import Any

from schemaguru.agent.dispatch import handle_tool_call
from schemaguru.agent.tools import ToolHost


def need(module: str, extra: str) -> Any:
    """Import ``module`` or explain which extra provides it."""
    try:
        return importlib.import_module(module)
    except ImportError as e:
        raise ImportError(
            f"this adapter needs {module!r}: pip install 'schemaguru[{extra}]'"
        ) from e


def bind(guru: ToolHost, name: str) -> Callable[..., str]:
    """A function running tool ``name`` only: the name is closed over, so no argument can
    reroute the call to another tool (an unexpected keyword is refused by the dispatcher)."""

    def run(**kwargs: Any) -> str:
        return call_json(guru, name, kwargs)

    run.__name__ = name
    return run


def call_json(guru: ToolHost, name: str, args: Any) -> str:
    """Run a tool and return its whole result as JSON text (``text`` plus structured fields)."""
    return json.dumps(handle_tool_call(guru, name, args), ensure_ascii=False)


__all__ = ["bind", "call_json", "need"]
