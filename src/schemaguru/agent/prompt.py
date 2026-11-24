"""The system-prompt fragment (spec 18.2): how to use the tools, the legend, global pitfalls;
with a question, the question's scoped view appended."""

from __future__ import annotations

from schemaguru.agent.dispatch import outbound_mode
from schemaguru.agent.tools import ToolHost, tool_definitions
from schemaguru.render.legend import LEGEND
from schemaguru.render.table_block import outbound
from schemaguru.render.view import ViewRenderer

USAGE = (
    "How to use them:",
    "- Call schema_context with the user's question before writing SQL; use only the tables, "
    "columns and joins it shows, and heed every ⚠ line.",
    "- Use search_tables when a table you need is not in the view, then describe_table for "
    "its full block.",
    "- Join only along routes find_join_path returns; a refusal means there is no safe route. "
    "Never guess a join key or a table name: a bare name that matches several tables comes "
    "back as an error listing the candidates.",
    "- Use column_values before filtering on a coded column; values may be masked by policy.",
    "- Use explain_term for business terms and metrics.",
)
CHECK_USAGE = (
    "- check_fact runs a claim on the warehouse under a spending budget; use it to confirm a "
    "number you rely on, not to explore."
)


def prompt_block(guru: ToolHost, question: str | None = None) -> str:
    """The prompt fragment; ``question`` appends its scoped view (``guru.context``)."""
    specs = tool_definitions(guru)
    names = {s["name"] for s in specs}
    lines = [
        "You can inspect the data warehouse with these tools:",
        *(f"- {s['name']}: {s['description']}" for s in specs),
        *USAGE,
        *([CHECK_USAGE] if "check_fact" in names else []),
    ]

    def stable(r: ViewRenderer) -> list[str]:
        return [b.text for b in r.global_blocks()]

    block = "\n\n".join(["\n".join(lines), *guru._with_renderer(stable), LEGEND])
    block = outbound(block, outbound_mode(guru))[0]
    if question is None:
        return block
    return block + "\n\n" + guru.context(question).text


__all__ = ["CHECK_USAGE", "USAGE", "prompt_block"]
