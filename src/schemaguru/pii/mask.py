"""Value masking (spec 11): replace personal values with their shape token."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Literal

from schemaguru.pii.detectors import first_match, name_spans, names_enabled
from schemaguru.pii.sweep import sweep, sweep_detail

_TOKENS = {
    "email": "<email>",
    "phone": "<phone>",
    "name": "<name>",
    "iban": "<iban>",
    "card": "<card>",
    "ip": "<ip>",
    "uuid": "<uuid>",
    "secret": "<secret>",
    "address": "<address>",
}


def mask_value(value: object) -> tuple[str, str | None]:
    """Return ``(masked_text, category)``; ``category`` is None when nothing was masked.

    ``None`` and blank strings are NULL for detection: ``("", None)``. A value whose whole
    text has a PII shape becomes that shape's token (``<hex{n}>`` for hex of n chars,
    ``<free_text len=N>`` for long prose). Otherwise any embedded hit (an email inside a short
    sentence) is masked in place, so a value never leaves with an identifier intact.
    """
    if value is None:
        return "", None
    text = value if isinstance(value, str) else str(value)
    if not text.strip():
        return "", None
    det = first_match(text)
    if det is not None:
        cat = det.category
        if cat == "free_text":
            return f"<free_text len={len(text.strip())}>", cat
        if cat == "hex":
            return f"<hex{len(text.strip())}>", cat
        return _TOKENS.get(cat, f"<{cat}>"), cat
    masked, hits = sweep_detail(text)
    if names_enabled():
        spans = name_spans(masked)
        if spans:
            out: list[str] = []
            pos = 0
            for a, b in spans:
                out.append(masked[pos:a])
                out.append("<name>")
                pos = b
            out.append(masked[pos:])
            masked = "".join(out)
            hits.append("name")
    if hits:
        return masked, hits[0]
    return text, None


def mask_free_text(text: str) -> tuple[str, int]:
    """Mask personal data *inside* prose (comments, messages); return ``(text, hits)``.

    The outbound :func:`~schemaguru.pii.sweep` (shapes: email, IBAN, card, ...) plus the
    embedded person-name masking ``mask_value`` uses (``name_spans`` when names are enabled).
    Only for free text: identifiers (table/column names) never go through name masking.
    """
    masked, hits = sweep(text)
    if names_enabled():
        spans = name_spans(masked)
        if spans:
            out: list[str] = []
            pos = 0
            for a, b in spans:
                out.append(masked[pos:a])
                out.append("<name>")
                pos = b
            out.append(masked[pos:])
            masked, hits = "".join(out), hits + len(spans)
    return masked, hits


def mask_text(text: str) -> str:
    """One stored string, masked: a whole-value shape becomes its token, prose is swept."""
    masked, cat = mask_value(text)
    if cat == "free_text":
        return mask_free_text(text)[0]
    return masked if cat is not None else text


_SQL_LITERAL = re.compile(r"[eEnN]?'(?:[^']|'')*'")


def sql_template(sql: str) -> str:
    """The SQL with every quoted string literal replaced by a ``?`` placeholder."""
    return _SQL_LITERAL.sub("?", sql)


_SQL_KEYS = frozenset({"sql", "query", "rendered_sql"})


def scrub_tree(node: Any, mode: Literal["masked", "raw", "none"], *, key: str | None = None) -> Any:
    """Deep-walk a JSON-like tree for storage (spec 11).

    SQL-valued keys keep only their template. ``raw`` keeps values; ``masked`` masks every
    string; ``none`` masks every string and replaces each value-bearing list (a list of objects
    with a ``value``) by its count under ``<key>_count``.
    """
    if isinstance(node, str):
        if key in _SQL_KEYS:
            return sql_template(node)
        return node if mode == "raw" else mask_text(node)
    if isinstance(node, Mapping):
        out: dict[str, Any] = {}
        for k, v in node.items():
            ks = str(k)
            if mode == "none" and _value_bearing(v):
                out[f"{ks}_count"] = len(v)
                continue
            out[ks] = scrub_tree(v, mode, key=ks)
        return out
    if isinstance(node, list | tuple):
        return [scrub_tree(v, mode, key=key) for v in node]
    return node


def _value_bearing(v: Any) -> bool:
    return (
        isinstance(v, list | tuple)
        and bool(v)
        and any(isinstance(i, Mapping) and "value" in i for i in v)
    )
