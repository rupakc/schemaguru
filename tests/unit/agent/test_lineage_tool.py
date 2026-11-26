"""The ``lineage`` tool (0.2.0 spec 4.8): listed last, value-policy result, refusals as SG803,
inherited by MCP and the adapters through ``guru.tools()``."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from schemaguru.agent import tool_definitions
from schemaguru.agent.prompt import USAGE
from schemaguru.guru import Guru
from schemaguru.model.facts import Fact, Provenance, Status
from schemaguru.model.kinds import FactKind
from schemaguru.model.lineage import column_lineage_value, table_lineage_value
from schemaguru.model.refs import ObjectRef
from tests.unit.agent.test_tools import SPEC_TOOLS


def _seed(guru: Guru) -> None:
    payments = ObjectRef(None, "shop", "payments")
    guru.store.put_facts(
        [
            Fact.make(
                FactKind.LINEAGE, payments, table_lineage_value(["shop.orders"], ["raw.pay"]),
                Provenance.DECLARED, Status.UNVERIFIED, source="dbt:manifest:model.shop.payments",
            ),
            Fact.make(
                FactKind.COLUMN_LINEAGE, ObjectRef(None, "shop", "payments", "amount"),
                column_lineage_value(["shop.orders.amount"], "cast"), Provenance.CATALOG,
                Status.UNVERIFIED, source="view_definition",
            ),
        ]
    )  # fmt: skip


def test_tool_listed_last_with_schema(shop_guru: Guru, no_seam_check: Any) -> None:
    specs = tool_definitions(shop_guru)
    assert specs[-1]["name"] == "lineage" and SPEC_TOOLS[-1] == "lineage"
    spec = specs[-1]
    assert spec["description"] == (
        "Which tables and columns a table or column is built from (upstream) or feeds "
        "(downstream), from dbt manifests, view definitions and overlays. Declared or parsed, "
        "never measured; not a join path."
    )
    schema = spec["input_schema"]
    assert schema["required"] == ["ref"]
    props = schema["properties"]
    assert props["ref"]["type"] == "string" and props["ref"]["maxLength"] == 512
    assert props["direction"]["enum"] == ["upstream", "downstream"]
    assert (props["depth"]["minimum"], props["depth"]["maximum"]) == (1, 5)
    assert props["level"]["enum"] == ["table", "column"]
    assert any("`lineage`" in line and "never a join path" in line for line in USAGE)
    assert "lineage" in shop_guru.prompt_block()


def test_tool_result_shape(shop_guru: Guru, no_seam_check: Any) -> None:
    _seed(shop_guru)
    out = shop_guru.handle_tool_call("lineage", {"ref": "payments"})
    assert "error" not in out, out
    assert json.loads(json.dumps(out)) == out
    assert set(out) == {"text", "ref", "direction", "level", "depth", "nodes", "edges",
                        "truncated", "cycles"}  # fmt: skip
    assert (out["ref"], out["direction"], out["level"], out["depth"]) == (
        "shop.payments", "upstream", "table", 2
    )  # fmt: skip
    assert out["edges"][0] == {
        "upstream": "raw.pay", "downstream": "shop.payments", "source": ["dbt:manifest"],
        "transform": None,
    }  # fmt: skip
    assert out["text"].startswith("Lineage upstream of shop.payments (table level, depth 2)")
    col = shop_guru.handle_tool_call("lineage", {"ref": "shop.payments.amount"})
    assert col["level"] == "column" and col["edges"][0]["transform"] == "cast"
    as_table = shop_guru.handle_tool_call(
        "lineage", {"ref": "shop.payments.amount", "level": "table", "depth": 1}
    )
    assert as_table["ref"] == "shop.payments" and as_table["depth"] == 1
    down = shop_guru.handle_tool_call("lineage", {"ref": "orders", "direction": "downstream"})
    assert [e["downstream"] for e in down["edges"]] == ["shop.payments"]
    none = shop_guru.handle_tool_call("lineage", {"ref": "shop.customers"})
    assert none["text"] == "No lineage recorded for shop.customers (upstream)."
    assert none["edges"] == [] and none["nodes"][0]["ref"] == "shop.customers"


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"ref": ""},
        {"ref": "x" * 513},
        {"ref": "shop.orders", "depth": 0},
        {"ref": "shop.orders", "depth": 6},
        {"ref": "shop.orders", "depth": "2"},
        {"ref": "shop.orders", "direction": "sideways"},
        {"ref": "shop.orders", "level": "row"},
        {"ref": "shop.orders", "level": "column"},
        {"ref": "shop.nowhere"},
        {"ref": "shop.orders", "extra": 1},
    ],
)
def test_bad_args_sg803(shop_guru: Guru, no_seam_check: Any, args: dict[str, Any]) -> None:
    out = shop_guru.handle_tool_call("lineage", args)
    assert out["error"]["code"] == "SG803", out
    assert out["text"].startswith("SG803 ")


def test_adapters_and_mcp_expose_lineage(shop_guru: Guru, no_seam_check: Any) -> None:
    pytest.importorskip("mcp")
    from mcp import Client

    from schemaguru.agent.adapters import anthropic as adapter
    from schemaguru.agent.mcp import build_server

    _seed(shop_guru)
    assert "lineage" in [t["name"] for t in adapter.tools(shop_guru)]
    use = {"type": "tool_use", "id": "t1", "name": "lineage", "input": {"ref": "payments"}}
    res = adapter.run_tool_use(shop_guru, use)
    assert not res["is_error"] and "shop.payments <- shop.orders" in res["content"]

    async def session() -> tuple[Any, Any]:
        async with Client(build_server(shop_guru)) as client:
            tools = await client.list_tools()
            called = await client.call_tool("lineage", {"ref": "payments"})
            return tools, called

    tools, called = asyncio.run(session())
    assert [t.name for t in tools.tools][-1] == "lineage"
    assert not called.is_error and called.structured_content["ref"] == "shop.payments"
