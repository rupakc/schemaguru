"""The prompt block (spec 18.2): usage, tools, legend, global pitfalls; a question's view."""

from __future__ import annotations

from typing import Any

from schemaguru.agent import prompt_block
from schemaguru.guru import Guru
from schemaguru.render.legend import LEGEND


def test_prompt_block_contains_legend_and_usage(shop_guru: Guru, no_seam_check: Any) -> None:
    block = prompt_block(shop_guru)
    assert LEGEND in block
    for name in ("schema_context", "search_tables", "describe_table", "find_join_path"):
        assert name in block
    assert "check_fact" not in block  # not offered when the tool is absent
    assert "Call schema_context" in block  # how to use the tools
    assert "never guess" in block.lower()
    assert shop_guru.prompt_block() == block


def test_prompt_block_with_question_embeds_view(shop_guru: Guru, no_seam_check: Any) -> None:
    block = prompt_block(shop_guru)
    with_q = prompt_block(shop_guru, "orders per customer")
    assert with_q.startswith(block)
    assert shop_guru.context("orders per customer").text in with_q


def test_prompt_block_mentions_check_fact_when_offered(shop_guru: Guru, seam_check: Any) -> None:
    assert "check_fact" in prompt_block(shop_guru)
