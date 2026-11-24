"""Output formats of a ContextView (spec 13.1)."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET

import pytest

from schemaguru.model.refs import ObjectRef
from schemaguru.render import ContextView, ViewBlock
from schemaguru.render.formats import format_view, register_format


def view() -> ContextView:
    return ContextView.build(
        'orders <&> "refunds"',
        (
            ViewBlock(True, "Legend: ⚠ hazard", kind="legend", title="Legend"),
            ViewBlock(
                False,
                "[shop.orders] a < b & c\n  order_id INTEGER",
                kind="table",
                title="shop.orders",
            ),
        ),
        tables=(ObjectRef(None, "shop", "orders"),),
    )


def test_text_joins_blocks_and_counts_tokens() -> None:
    v = view()
    assert v.text == "Legend: ⚠ hazard\n\n[shop.orders] a < b & c\n  order_id INTEGER"
    assert v.tokens == -(-len(v.text) // 4)


def test_json_is_canonical_and_complete() -> None:
    v = view()
    data = json.loads(v.to_json())
    assert data["question"] == v.question
    assert data["fingerprint"] == v.fingerprint
    assert data["tables"] == ["shop.orders"]
    assert [b["kind"] for b in data["blocks"]] == ["legend", "table"]
    assert data["blocks"][1]["stable"] is False
    assert v.to_json() == view().to_json()  # byte-deterministic
    assert ", " not in v.to_json().split('"text"')[0]  # compact separators


def test_xml_is_well_formed_and_escaped() -> None:
    root = ET.fromstring(view().to_xml())
    assert root.tag == "context"
    assert root.get("question") == 'orders <&> "refunds"'
    blocks = root.findall("block")
    assert blocks[1].get("name") == "shop.orders"
    assert blocks[1].text == "[shop.orders] a < b & c\n  order_id INTEGER"


def test_markdown_has_sections_and_keeps_alignment() -> None:
    md = view().to_markdown()
    assert md.startswith("# Context: orders")
    assert "## shop.orders\n\n```text\n[shop.orders] a < b & c\n  order_id INTEGER\n```" in md


def test_register_format() -> None:
    register_format("test-upper", lambda v: v.text.upper())
    try:
        assert format_view(view(), "test-upper").startswith("LEGEND")
        with pytest.raises(ValueError, match="already registered"):
            register_format("test-upper", lambda v: "")
    finally:
        from schemaguru.render import formats

        formats._FORMATS.pop("test-upper", None)
    assert format_view(view(), "text") == view().text
    with pytest.raises(KeyError, match="unknown format"):
        format_view(view(), "nope")
