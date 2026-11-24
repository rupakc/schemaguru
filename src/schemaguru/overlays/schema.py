"""Overlay data types and the filter syntax check (spec 14)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from schemaguru.model.refs import ObjectRef

OVERLAY_VERSION = 1

TOP_KEYS = (
    "version",
    "warehouse",
    "tables",
    "chains",
    "waypoints",
    "glossary",
    "never_query",
    "waivers",
)
TABLE_KEYS = (
    "description",
    "use_cases",
    "entity_type",
    "soft_delete",
    "pitfalls",
    "columns",
    "joins",
)
COLUMN_KEYS = ("description", "values", "values_split", "type_note")
PITFALL_KEYS = ("rule", "fix", "severity")
JOIN_KEYS = ("to", "from", "cardinality", "cast_to")
CHAIN_KEYS = ("name", "because", "steps")
STEP_KEYS = ("filter", "table", "note")
WAYPOINT_KEYS = ("pair", "node", "require", "forbid", "because")
GLOSSARY_KEYS = ("term", "definition", "maps_to")
NEVER_QUERY_KEYS = ("table", "reason")
WAIVER_KEYS = ("fact", "reason", "expires")

CARDINALITIES = ("N:1", "1:1", "1:N", "N:M")
SEVERITIES = ("warn", "critical")

# Global facts (glossary) have no table; they hang off this sentinel subject.
GLOBAL_SUBJECT = ObjectRef(None, None, "_global")


@dataclass(frozen=True)
class WaypointSpec:
    """A human waypoint rule (spec 9.5); consumed by the graph layer."""

    scope: Literal["pair", "node"]
    tables: tuple[ObjectRef, ...]
    require: tuple[str, ...]
    forbid: tuple[str, ...]
    because: str
    source: str


def check_filter_syntax(text: str) -> str | None:
    """Syntax-only filter check: a problem description, or ``None`` when the text looks sound.

    TODO(Task 14): replace with ``render.fragments.validate_filter`` (FactLang ``parse_filter``)
    once that seam exists; this check only catches empty text, ``;``, and unbalanced quotes or
    parentheses.
    """
    if not text.strip():
        return "filter is empty"
    depth = 0
    quote: str | None = None
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if quote is not None:
            if ch == quote:
                if i + 1 < n and text[i + 1] == quote:  # doubled quote is an escape
                    i += 2
                    continue
                quote = None
        elif ch in "'\"`":
            quote = ch
        elif ch == ";":
            return "filter must not contain ';'"
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                return "unbalanced parentheses: unexpected ')'"
        i += 1
    if quote is not None:
        return f"unterminated {quote} quote"
    if depth != 0:
        return "unbalanced parentheses: missing ')'"
    return None
