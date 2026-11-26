"""Tool definitions (spec 18.1): exactly the spec's tools, valid JSON schemas, check_fact gated."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from schemaguru.agent import tool_definitions
from schemaguru.guru import Guru

SPEC_TOOLS = [
    "schema_context",
    "search_tables",
    "describe_table",
    "find_join_path",
    "column_values",
    "check_fact",
    "explain_term",
    "lineage",
]

_TYPES = {"object", "string", "integer", "number", "boolean", "array"}


def _structurally_valid(schema: Any) -> None:
    assert isinstance(schema, dict) and schema["type"] == "object"
    assert schema.get("additionalProperties") is False
    props = schema["properties"]
    assert set(schema["required"]) <= set(props)
    for name, prop in props.items():
        assert isinstance(name, str) and prop["type"] in _TYPES
        assert isinstance(prop.get("description"), str) and prop["description"]


def test_tool_schemas_valid_json_schema(shop_guru: Guru, seam_check: Any) -> None:
    specs = tool_definitions(shop_guru)
    assert [s["name"] for s in specs] == SPEC_TOOLS
    for s in specs:
        assert set(s) == {"name", "description", "input_schema"}
        assert s["description"].strip()
        _structurally_valid(s["input_schema"])
    jsonschema = pytest.importorskip("jsonschema")
    for s in specs:
        jsonschema.Draft202012Validator.check_schema(s["input_schema"])
    # the façade method is the same list
    assert shop_guru.tools() == specs


def test_check_fact_takes_factlangs_definition(shop_guru: Guru, seam_check: Any) -> None:
    spec = next(s for s in tool_definitions(shop_guru) if s["name"] == "check_fact")
    assert spec["description"].startswith(seam_check["description"])
    assert spec["input_schema"]["required"] == ["claims"]


def test_check_fact_omitted_without_factlang_agent(shop_guru: Guru, no_seam_check: None) -> None:
    names = [s["name"] for s in tool_definitions(shop_guru)]
    assert names == [n for n in SPEC_TOOLS if n != "check_fact"]


def test_check_fact_omitted_when_config_denies(shop_guru: Guru, seam_check: Any) -> None:
    shop_guru.config = replace(shop_guru.config, agent_checks="deny")
    assert "check_fact" not in [s["name"] for s in tool_definitions(shop_guru)]


def test_tool_definitions_are_fresh_copies(shop_guru: Guru, seam_check: Any) -> None:
    a = tool_definitions(shop_guru)
    a[0]["input_schema"]["properties"].clear()
    assert tool_definitions(shop_guru)[0]["input_schema"]["properties"]


def test_check_fact_is_off_by_default(tmp_path: Any, shop_duckdb: Any, seam_check: Any) -> None:
    """Spec 8.2: an agent question never spends by itself; checks are an explicit opt-in."""
    from schemaguru.agent import handle_tool_call
    from schemaguru.config import Config
    from tests.unit.agent.conftest import make_shop_guru

    assert Config().agent_checks == "deny"
    g = make_shop_guru(tmp_path, shop_duckdb[0], config=Config())
    try:
        assert "check_fact" not in [s["name"] for s in tool_definitions(g)]
        out = handle_tool_call(g, "check_fact", {"claims": "ROWS shop.orders VALUE >=1"})
        assert out["error"]["code"] == "SG803" and "agent_checks" in out["error"]["message"]
    finally:
        g.close()


def test_string_arguments_are_capped(shop_guru: Guru, seam_check: Any) -> None:
    from schemaguru.agent.tools import MAX_NAME, MAX_TEXT

    caps = {
        s["name"]: {k: p.get("maxLength") for k, p in s["input_schema"]["properties"].items()}
        for s in tool_definitions(shop_guru)
    }
    assert caps["describe_table"]["table"] == MAX_NAME == 512
    assert caps["find_join_path"] == {"from": 512, "to": 512}
    assert caps["column_values"]["column"] == 512
    assert caps["schema_context"]["question"] == MAX_TEXT == 20_000
    assert caps["explain_term"]["term"] == 20_000
    assert caps["check_fact"]["claims"] == 20_000
    assert caps["lineage"]["ref"] == MAX_NAME == 512
