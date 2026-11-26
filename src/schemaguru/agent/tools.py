"""Agent tool definitions (spec 18.1): JSON-schema specs over a :class:`ToolHost` (the Guru).

Exactly the spec's tools, in a fixed order. ``check_fact`` takes its description and input
schema from FactLang's ``factlang_check`` (through the FactLang seam, ``measure.flbridge``) and
is omitted when the installed FactLang publishes no agent tools, or unless
``Config.agent_checks == "allow"`` (default "deny": it spends warehouse money on an agent's
request, spec 8.2).
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any, Protocol, TypeVar

from schemaguru.config import Config
from schemaguru.graph.search import PathResult
from schemaguru.knowledge.lineage import MAX_DEPTH, LineageResult
from schemaguru.measure import flbridge
from schemaguru.measure.backend import MeasureBackend
from schemaguru.measure.budget import BudgetGuard
from schemaguru.model.refs import ObjectRef
from schemaguru.render.view import ContextView, ViewRenderer
from schemaguru.scope.retrievers import Retriever
from schemaguru.store.protocol import Store

_T = TypeVar("_T")

TOOL_NAMES = (
    "schema_context",
    "search_tables",
    "describe_table",
    "find_join_path",
    "column_values",
    "check_fact",
    "explain_term",
    "lineage",
)
CHECK_PREAMBLE = "Runs on the warehouse under the measurement budget; use it sparingly. "


class ToolHost(Protocol):
    """What the agent surface needs from a Guru (a structural type, so the agent package does
    not import the façade at module level: both are layer 7)."""

    store: Store
    retriever: Retriever
    backend: MeasureBackend

    @property
    def config(self) -> Config: ...

    def table_ref(self, name: str | ObjectRef) -> ObjectRef: ...

    def column_ref(self, name: str | ObjectRef) -> ObjectRef: ...

    def context(
        self,
        question: str,
        *,
        budget_tokens: int = 4000,
        profile: str = "agent",
        format: str = "text",
    ) -> ContextView: ...

    def find_path(
        self, a: str | ObjectRef, b: str | ObjectRef, *, k: int | None = None
    ) -> PathResult: ...

    def lineage(
        self,
        ref: str | ObjectRef,
        direction: str = "upstream",
        depth: int = 2,
        level: str | None = None,
    ) -> LineageResult: ...

    def _with_renderer(self, fn: Callable[[ViewRenderer], _T], profile: str = "agent") -> _T: ...

    def _agent_guard(self) -> BudgetGuard: ...


MAX_NAME = 512  # a table or column name
MAX_TEXT = 20_000  # a question, query, term or claim text


def _string(description: str, max_length: int = MAX_NAME) -> dict[str, Any]:
    return {"type": "string", "minLength": 1, "maxLength": max_length, "description": description}


def _spec(
    name: str, description: str, props: dict[str, dict[str, Any]], required: list[str]
) -> dict[str, Any]:
    schema = {
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }
    return {"name": name, "description": description, "input_schema": schema}


_BASE: dict[str, dict[str, Any]] = {
    "schema_context": _spec(
        "schema_context",
        "The warehouse tables, columns, measured joins and hazards relevant to a question. "
        "Call this first, before writing SQL.",
        {
            "question": _string("the user's question, in natural language", MAX_TEXT),
            "budget_tokens": {
                "type": "integer",
                "minimum": 200,
                "maximum": 100000,
                "description": "token budget of the returned view (default 4000)",
            },
        },
        ["question"],
    ),
    "search_tables": _spec(
        "search_tables",
        "Ranked tables matching a text query, each with a one-line description.",
        {
            "query": _string(
                "words to search table names, comments and descriptions for", MAX_TEXT
            ),
            "k": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "description": "how many tables to return (default 10)",
            },
        },
        ["query"],
    ),
    "describe_table": _spec(
        "describe_table",
        "Everything known about one table: columns, keys, joins, values, pitfalls.",
        {"table": _string("table name: schema.table, or a bare name when it is unambiguous")},
        ["table"],
    ),
    "find_join_path": _spec(
        "find_join_path",
        "Measured join routes between two tables with join keys and a SQL snippet, or the "
        "reason there is none. Never invent a join this tool does not return.",
        {
            "from": _string("the table to start from"),
            "to": _string("the table to reach"),
        },
        ["from", "to"],
    ),
    "column_values": _spec(
        "column_values",
        "Observed and declared values of one column, as the value policy allows them.",
        {"column": _string("column as table.column (schema.table.column when ambiguous)")},
        ["column"],
    ),
    "explain_term": _spec(
        "explain_term",
        "What a business term or metric means and which tables it maps to.",
        {"term": _string("the business term or metric name", MAX_TEXT)},
        ["term"],
    ),
    "lineage": _spec(
        "lineage",
        "Which tables and columns a table or column is built from (upstream) or feeds "
        "(downstream), from dbt manifests, view definitions and overlays. Declared or parsed, "
        "never measured; not a join path.",
        {
            "ref": _string("table or column: schema.table or schema.table.column"),
            "direction": {
                "type": "string",
                "enum": ["upstream", "downstream"],
                "description": "upstream: what it is built from (default); downstream: what "
                "reads it",
            },
            "depth": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_DEPTH,
                "description": f"hops to follow, 1..{MAX_DEPTH} (default 2)",
            },
            "level": {
                "type": "string",
                "enum": ["table", "column"],
                "description": "table or column lineage (default: the level of `ref`)",
            },
        },
        ["ref"],
    ),
}


def check_spec(guru: ToolHost) -> dict[str, Any] | None:
    """The ``check_fact`` spec (FactLang's definition), or None when it is not offered."""
    if guru.config.agent_checks != "allow":
        return None
    fl = flbridge.check_tool_definition()
    if fl is None:
        return None
    schema = copy.deepcopy(fl["input_schema"])
    if not isinstance(schema.get("properties"), dict):
        return None
    schema.setdefault("type", "object")
    schema.setdefault("required", [])
    schema.setdefault("additionalProperties", False)
    for prop in schema["properties"].values():
        if isinstance(prop, dict):
            prop.setdefault("description", "FactLang claim text")
            if prop.get("type") == "string":
                prop.setdefault("maxLength", MAX_TEXT)
    cap = guru.config.check_max_claims
    limit = "" if cap is None else f" A call may carry at most {cap} claims."
    return {
        "name": "check_fact",
        "description": f"{fl['description']} {CHECK_PREAMBLE}{limit}".strip(),
        "input_schema": schema,
    }


def tool_definitions(guru: ToolHost) -> list[dict[str, Any]]:
    """The agent tools, ``{"name", "description", "input_schema"}`` each (fresh copies)."""
    out: list[dict[str, Any]] = []
    for name in TOOL_NAMES:
        spec = check_spec(guru) if name == "check_fact" else copy.deepcopy(_BASE[name])
        if spec is not None:
            out.append(spec)
    return out


__all__ = ["MAX_NAME", "MAX_TEXT", "TOOL_NAMES", "ToolHost", "check_spec", "tool_definitions"]
