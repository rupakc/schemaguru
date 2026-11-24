"""Framework adapters (spec 18.4): each maps ``tool_definitions`` only; each skips without its
package. Every adapter is exercised through its framework's real call path."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from schemaguru.agent import tool_definitions
from schemaguru.guru import Guru


def _names(guru: Guru) -> list[str]:
    return [s["name"] for s in tool_definitions(guru)]


def test_anthropic_tools_and_run_tool_use(shop_guru: Guru, no_seam_check: Any) -> None:
    from schemaguru.agent.adapters import anthropic as adapter

    tools = adapter.tools(shop_guru)
    assert tools == tool_definitions(shop_guru)
    block = SimpleNamespace(
        type="tool_use", id="toolu_1", name="describe_table", input={"table": "orders"}
    )
    out = adapter.run_tool_use(shop_guru, block)
    assert out["type"] == "tool_result" and out["tool_use_id"] == "toolu_1"
    assert "[shop.orders]" in out["content"] and out["is_error"] is False
    err = adapter.run_tool_use(
        shop_guru, {"type": "tool_use", "id": "toolu_2", "name": "nope", "input": {}}
    )
    assert err["is_error"] is True and err["content"].startswith("SG803 ")
    sdk = pytest.importorskip("anthropic")
    from anthropic.types import ToolParam, ToolUseBlock

    for t in tools:  # the dicts are valid SDK tool params
        assert set(t) <= set(ToolParam.__annotations__)
    real = ToolUseBlock(
        id="toolu_3", name="search_tables", input={"query": "orders"}, type="tool_use"
    )
    assert "shop.orders" in adapter.run_tool_use(shop_guru, real)["content"]
    assert sdk.__version__
    runner = {t.name: t for t in adapter.runner_tools(shop_guru)}
    assert list(runner) == _names(shop_guru)
    assert runner["search_tables"].to_dict()["input_schema"] == tools[1]["input_schema"]
    assert "[shop.orders]" in runner["describe_table"].call({"table": "orders"})


def test_langchain_tools(shop_guru: Guru, no_seam_check: Any) -> None:
    pytest.importorskip("langchain_core")
    from langchain_core.tools import BaseTool
    from langchain_core.utils.function_calling import convert_to_openai_tool

    from schemaguru.agent.adapters import langchain as adapter

    tools = adapter.tools(shop_guru)
    assert all(isinstance(t, BaseTool) for t in tools)
    assert [t.name for t in tools] == _names(shop_guru)
    by_name = {t.name: t for t in tools}
    out = json.loads(by_name["describe_table"].invoke({"table": "orders"}))
    assert "[shop.orders]" in out["text"]
    fn = convert_to_openai_tool(by_name["search_tables"])["function"]
    assert fn["parameters"]["required"] == ["query"]
    assert set(fn["parameters"]["properties"]) == {"query", "k"}
    bad = json.loads(by_name["describe_table"].invoke({"table": "nowhere"}))
    assert bad["error"]["code"] == "SG803"
    docs = adapter.retriever(shop_guru, k=2).invoke("orders")
    assert docs and "[shop.orders]" in docs[0].page_content
    assert docs[0].metadata["table"] == "shop.orders"


def test_llamaindex_tool_spec(shop_guru: Guru, no_seam_check: Any) -> None:
    pytest.importorskip("llama_index.core")
    from llama_index.core.tools import FunctionTool

    from schemaguru.agent.adapters import llamaindex as adapter

    tools = adapter.tool_spec(shop_guru)
    assert all(isinstance(t, FunctionTool) for t in tools)
    assert [t.metadata.name for t in tools] == _names(shop_guru)
    by_name = {t.metadata.name: t for t in tools}
    params = by_name["search_tables"].metadata.get_parameters_dict()
    assert params["required"] == ["query"] and set(params["properties"]) == {"query", "k"}
    out = by_name["describe_table"].call(table="orders")
    assert "[shop.orders]" in json.loads(out.content)["text"]
    assert json.loads(out.raw_output)["table"] == "shop.orders"


def test_openai_agents_function_tools(shop_guru: Guru, no_seam_check: Any) -> None:
    pytest.importorskip("agents")
    from agents import FunctionTool

    from schemaguru.agent.adapters import openai_agents as adapter

    tools = adapter.function_tools(shop_guru)
    assert all(isinstance(t, FunctionTool) for t in tools)
    assert [t.name for t in tools] == _names(shop_guru)
    by_name = {t.name: t for t in tools}
    t = by_name["describe_table"]
    assert t.params_json_schema == tool_definitions(shop_guru)[2]["input_schema"]
    out = asyncio.run(t.on_invoke_tool(None, json.dumps({"table": "orders"})))  # type: ignore[arg-type]
    assert "[shop.orders]" in json.loads(out)["text"]
    bad = asyncio.run(t.on_invoke_tool(None, "{not json"))  # type: ignore[arg-type]
    assert json.loads(bad)["error"]["code"] == "SG803"


# --- a model-supplied argument can never reroute a call to another tool -----------------------

REROUTE = {"table": "orders", "_name": "explain_term"}


def _refused_or_same(raw: str | dict[str, Any]) -> None:
    out = json.loads(raw) if isinstance(raw, str) else raw
    if "error" in out:
        assert out["error"]["code"] == "SG803" and "unexpected argument" in out["error"]["message"]
    else:
        assert out["table"] == "shop.orders"  # still describe_table, never explain_term
    assert "entries" not in out


def test_anthropic_name_argument_cannot_reroute(shop_guru: Guru, no_seam_check: Any) -> None:
    from schemaguru.agent.adapters import anthropic as adapter

    block = {"type": "tool_use", "id": "t", "name": "describe_table", "input": REROUTE}
    out = adapter.run_tool_use(shop_guru, block)
    assert out["is_error"] and "unexpected argument" in out["content"]
    pytest.importorskip("anthropic")
    runner = {t.name: t for t in adapter.runner_tools(shop_guru)}
    text = runner["describe_table"].call(REROUTE)
    assert text.startswith("SG803 ") and "unexpected argument" in text


def test_langchain_name_argument_cannot_reroute(shop_guru: Guru, no_seam_check: Any) -> None:
    pytest.importorskip("langchain_core")
    from schemaguru.agent.adapters import langchain as adapter

    tool = {t.name: t for t in adapter.tools(shop_guru)}["describe_table"]
    _refused_or_same(tool.invoke(REROUTE))
    for bad in (0, 101, "3", True):
        with pytest.raises(ValueError):
            adapter.retriever(shop_guru, k=bad)  # type: ignore[arg-type]


def test_llamaindex_name_argument_cannot_reroute(shop_guru: Guru, no_seam_check: Any) -> None:
    pytest.importorskip("llama_index.core")
    from schemaguru.agent.adapters import llamaindex as adapter

    tool = {t.metadata.name: t for t in adapter.tool_spec(shop_guru)}["describe_table"]
    _refused_or_same(tool.call(**REROUTE).content)


def test_openai_agents_name_argument_cannot_reroute(shop_guru: Guru, no_seam_check: Any) -> None:
    pytest.importorskip("agents")
    from schemaguru.agent.adapters import openai_agents as adapter

    tool = {t.name: t for t in adapter.function_tools(shop_guru)}["describe_table"]
    _refused_or_same(asyncio.run(tool.on_invoke_tool(None, json.dumps(REROUTE))))  # type: ignore[arg-type]
