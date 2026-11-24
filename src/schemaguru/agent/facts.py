"""Stored facts of one subject as they may leave the library (spec 11, 17).

This is the agent layer's single definition of an outbound fact. The HTTP ``/facts`` route
returns it and nothing else, so the server cannot drift from the tools. Every field is passed
through the same helpers ``describe_table`` and ``column_values`` use, applied under the table's
value mode (``ValuePresenter.mode``, the policy ``outbound_mode`` applies to one table):

* **observed or declared values** (``VALUES`` facts, plus any ``values`` / ``observed`` /
  ``max_value`` / ``min_value`` / ``sample`` field): ``ValuePresenter``. A PII column shows a
  whole shape token or ``<masked>``; in ``none`` mode only counts are shown.
* **predicates** (``predicate``, ``filter``): ``ValuePresenter.pred``, i.e. ``predicate_text``.
  ``none`` replaces every literal with ``?``.
* **other text**: ``ValuePresenter.free`` (``mask_free_text``). In ``none`` mode quoted literals
  also become ``?``, as in tool error messages.
* **evidence**: the tools' allow-list (``_outbound_evidence``).
* **last**: the outbound shape sweep over everything except the fact ``id`` (a SHA-256 id is not
  data, and a hex sweep would mangle it).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from schemaguru.agent.dispatch import (
    _json_safe,
    _outbound_evidence,
    _outbound_tree,
    _value_entry,
)
from schemaguru.agent.tools import ToolHost
from schemaguru.model.facts import Fact
from schemaguru.model.refs import ObjectRef
from schemaguru.render.table_block import ValuePresenter, predicate_text
from schemaguru.render.view import ViewRenderer

_VALUE_FIELDS = frozenset(
    {"values", "observed", "max_value", "min_value", "sample", "samples", "examples", "top"}
)
_PREDICATE_FIELDS = frozenset({"predicate", "filter", "where"})
_META = ("kind", "provenance", "status", "method", "observed_at", "snapshot_id", "source")


def _shown(p: ValuePresenter, column: str | None, node: Any) -> Any:
    """Values as ``ValuePresenter.shown`` renders them (``none``: a count)."""
    if p.mode == "none":
        return {"hidden": len(node) if isinstance(node, list) else 1}
    if isinstance(node, list):
        return [_shown(p, column, v) for v in node]
    if isinstance(node, Mapping):
        return {str(k): _shown(p, column, v) for k, v in node.items()}
    if isinstance(node, bool | int | float) or node is None:
        return node
    return p.shown(column or "", node)


def _text(p: ValuePresenter, text: str) -> str:
    out = p.free(text)
    return predicate_text(out, "none")[0] if p.mode == "none" else out


def _field(p: ValuePresenter, column: str | None, key: str | None, node: Any) -> Any:
    if key in _VALUE_FIELDS:
        return _shown(p, column, node)
    if isinstance(node, str):
        return p.pred(node) if key in _PREDICATE_FIELDS else _text(p, node)
    if isinstance(node, list):
        return [_field(p, column, key, v) for v in node]
    if isinstance(node, Mapping):
        return {str(k): _field(p, column, str(k), v) for k, v in node.items()}
    return node


def present_fact(p: ValuePresenter, f: Fact) -> dict[str, Any]:
    """One fact as it leaves, under ``p``'s table and value mode."""
    subject = f.subject.canonical() if isinstance(f.subject, ObjectRef) else str(f.subject)
    column = f.subject.column if isinstance(f.subject, ObjectRef) else None
    raw = _json_safe(f.to_dict())
    out: dict[str, Any] = {k: raw.get(k) for k in _META}
    out["subject"] = subject
    value = dict(f.value)
    col = column or (value.get("column") if isinstance(value.get("column"), str) else None)
    if f.kind.name == "VALUES" and column is not None:
        out["value"] = _value_entry(p, column, f)
    else:
        out["value"] = _field(p, col, None, _json_safe(value))
    if isinstance(out.get("source"), str):
        out["source"] = _text(p, out["source"])
    if f.evidence is not None:
        out["evidence"] = _outbound_evidence(f.evidence, p.mode)
    swept: dict[str, Any] = _outbound_tree(out, p.mode)
    return {"id": f.id, **swept}


def outbound_facts(
    guru: ToolHost, subject: ObjectRef, kinds: Iterable[str] | None = None
) -> dict[str, Any]:
    """The stored facts of ``subject`` (a table or a column), each through :func:`present_fact`."""
    table = subject.table_ref()
    kind_list = None if kinds is None else list(kinds)

    def build(r: ViewRenderer) -> dict[str, Any]:
        p = ValuePresenter(r.knowledge(table))
        facts = [present_fact(p, f) for f in guru.store.facts_for([subject], kind_list)]
        return {
            "subject": subject.canonical(),
            "mode": p.mode,
            "facts": facts,
            "text": f"{len(facts)} fact(s) for {subject.canonical()}",
        }

    result: dict[str, Any] = guru._with_renderer(build)
    return result


__all__ = ["outbound_facts", "present_fact"]
