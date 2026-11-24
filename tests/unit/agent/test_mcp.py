"""The MCP server (spec 18.3): tools and ``schema://tables/{ref}`` over an in-process session."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from schemaguru.agent import tool_definitions
from schemaguru.guru import Guru

pytest.importorskip("mcp")


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_mcp_in_process_session(shop_guru: Guru, no_seam_check: Any) -> None:
    from mcp import Client

    from schemaguru.agent.mcp import build_server

    server = build_server(shop_guru)

    async def session() -> dict[str, Any]:
        async with Client(server) as client:
            tools = await client.list_tools()
            ok = await client.call_tool("describe_table", {"table": "orders"})
            bad = await client.call_tool("describe_table", {"table": "nowhere"})
            templates = await client.list_resource_templates()
            resources = await client.list_resources()
            res = await client.read_resource("schema://tables/shop.orders")
            return {
                "tools": tools,
                "ok": ok,
                "bad": bad,
                "templates": templates,
                "resources": resources,
                "res": res,
            }

    out = _run(session())
    names = [t.name for t in out["tools"].tools]
    assert names == [s["name"] for s in tool_definitions(shop_guru)]
    first = out["tools"].tools[0]
    assert first.input_schema == tool_definitions(shop_guru)[0]["input_schema"]
    ok = out["ok"]
    assert not ok.is_error and "[shop.orders]" in ok.content[0].text
    assert ok.structured_content["table"] == "shop.orders"
    assert json.loads(json.dumps(ok.structured_content)) == ok.structured_content
    bad = out["bad"]
    assert bad.is_error and bad.structured_content["error"]["code"] == "SG803"
    assert [t.uri_template for t in out["templates"].resource_templates] == [
        "schema://tables/{ref}"
    ]
    assert "schema://tables/shop.orders" in [str(r.uri) for r in out["resources"].resources]
    assert "[shop.orders]" in out["res"].contents[0].text


def test_serve_is_stdio(monkeypatch: pytest.MonkeyPatch, shop_guru: Guru) -> None:
    """``serve`` wires the server to the SDK's stdio transport (not run: it would block)."""
    import contextlib

    import mcp.server.stdio as stdio

    from schemaguru.agent import mcp as sg_mcp

    seen: dict[str, Any] = {}

    @contextlib.asynccontextmanager
    async def fake_stdio() -> Any:
        seen["stdio"] = True
        yield ("r", "w")

    async def fake_run(self: Any, read: Any, write: Any, options: Any, **kw: Any) -> None:
        seen["run"] = (read, write)

    monkeypatch.setattr(stdio, "stdio_server", fake_stdio)
    from mcp.server.lowlevel import Server

    monkeypatch.setattr(Server, "run", fake_run)
    sg_mcp.serve(shop_guru)
    assert seen == {"stdio": True, "run": ("r", "w")}


def test_mcp_over_real_stdio(tmp_path: Any, shop_guru: Guru) -> None:
    """A subprocess serves a store over stdio; the SDK's stdio client talks to it."""
    import os
    import sys
    from pathlib import Path

    from mcp import Client, StdioServerParameters

    store = Path(shop_guru.store.path)  # type: ignore[attr-defined]
    script = (
        "import sys\n"
        "from schemaguru.guru import Guru\n"
        "from schemaguru.agent.mcp import serve\n"
        "serve(Guru(store=sys.argv[1]))\n"
    )
    src = str(Path(__file__).resolve().parents[3] / "src")
    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", script, str(store)],
        env={**os.environ, "PYTHONPATH": src},
    )

    async def session() -> Any:
        async with Client(params) as client:
            tools = await client.list_tools()
            res = await client.call_tool("search_tables", {"query": "orders", "k": 2})
            return tools, res

    tools, res = _run(session())
    assert "describe_table" in [t.name for t in tools.tools]
    assert not res.is_error and "shop.orders" in res.content[0].text


def test_mcp_resource_errors_are_mcp_errors(shop_guru: Guru) -> None:
    from mcp import Client
    from mcp.shared.exceptions import MCPError

    from schemaguru.agent.mcp import build_server, resource_ref

    assert resource_ref("schema://tables/shop.orders?x=1#top") == "shop.orders"
    assert resource_ref("schema://tables/shop%2Eorders") == "shop.orders"
    assert resource_ref("schema://tables/") is None and resource_ref("file:///etc") is None

    async def session() -> list[Any]:
        out: list[Any] = []
        async with Client(build_server(shop_guru)) as client:
            ok = await client.read_resource("schema://tables/shop.orders?x=1#top")
            out.append(ok.contents[0].text)
            for uri in ("schema://tables/nowhere", "schema://other/x", "schema://tables/"):
                try:
                    await client.read_resource(uri)
                    out.append(None)
                except MCPError as e:
                    out.append(e)
        return out

    ok, *errors = _run(session())
    assert "[shop.orders]" in ok
    assert all(isinstance(e, MCPError) for e in errors), errors
    assert all(e.code == -32602 for e in errors)
    assert errors[0].message.startswith("SG803 ") and errors[0].data["error"]["code"] == "SG803"


def test_mcp_uris_are_normalised_and_errors_echo_no_values(planted: Any) -> None:
    from mcp import Client
    from mcp.shared.exceptions import MCPError

    from schemaguru.agent.mcp import build_server

    g = planted("masked")

    async def session() -> list[Any]:
        out: list[Any] = []
        async with Client(build_server(g)) as client:
            ok = await client.read_resource("schema://tables/customers?x=Quentin Shapeless#f")
            out.append(ok)
            for uri in (
                "schema://tables/nope.Bartholomew Kipling",
                "bogus://Yolanda Brightwater",
                "schema://tables/" + "Z" * 600,
            ):
                try:
                    await client.read_resource(uri)
                    out.append(None)
                except MCPError as e:
                    out.append(e)
        return out

    ok, missing, bogus, long = _run(session())
    assert [str(c.uri) for c in ok.contents] == ["schema://tables/shop.customers"]
    assert missing.data["uri"] == "schema://tables/?" and bogus.data["uri"] == "bogus://"
    assert long.data["uri"] == "schema://tables/?"
    for e in (missing, bogus, long):
        dumped = json.dumps(e.data) + e.message
        assert "Bartholomew" not in dumped and "Yolanda" not in dumped and "ZZZ" not in dumped


def test_mcp_raw_mode_echoes_the_ref_cut_to_200(planted: Any) -> None:
    from mcp import Client
    from mcp.shared.exceptions import MCPError

    from schemaguru.agent.mcp import build_server

    g = planted("raw")

    async def session() -> Any:
        async with Client(build_server(g)) as client:
            try:
                await client.read_resource("schema://tables/" + "Z" * 600 + "?q=1")
            except MCPError as e:
                return e
        return None

    e = _run(session())
    assert e.data["uri"] == "schema://tables/" + "Z" * 200
