"""The LLM provider protocol and the draft output schema (spec 10.1, 10.2).

``DRAFT_SCHEMA`` is the JSON Schema every reply must satisfy. It is validated here with a small
stdlib validator (no ``jsonschema`` dependency): the subset used is ``type`` (incl. a list with
``"null"``), ``properties``, ``required``, ``additionalProperties: false``, ``items``,
``maxItems``, ``enum`` and ``minLength``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class LLMProvider(Protocol):
    """Spec 10.1, exactly. Providers MAY also expose ``model: str`` and ``last_usage: Usage``."""

    name: str

    def complete_json(
        self, system: str, user: str, schema: dict[str, Any], *, max_tokens: int
    ) -> dict[str, Any]: ...

    async def acomplete_json(
        self, system: str, user: str, schema: dict[str, Any], *, max_tokens: int
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class Usage:
    """Token usage of one provider call, as the provider reported it."""

    input_tokens: int
    output_tokens: int


class InvalidReply(ValueError):
    """A provider reply that is not a JSON object matching the schema (retried once)."""


def _nullable(t: str) -> list[str]:
    return [t, "null"]


def _obj(props: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": props,
        "required": list(props),
        "additionalProperties": False,
    }


_STR = {"type": "string", "minLength": 1}
_COLS = {"type": "array", "items": _STR}

TABLE_DRAFT_SCHEMA: dict[str, Any] = _obj(
    {
        "table": {**_STR, "description": "the table name exactly as given in the input"},
        "description": {**_STR, "description": "what one row is and what the table holds"},
        "columns": {
            "type": "array",
            "items": _obj({"name": _STR, "description": _STR}),
        },
        "use_cases": {"type": "array", "items": _STR, "maxItems": 5},
        "entity_type": {
            "type": _nullable("object"),
            "properties": {"entity": _STR, "key": _COLS},
            "required": ["entity", "key"],
            "additionalProperties": False,
        },
        "pitfalls": {
            "type": "array",
            "items": _obj(
                {
                    "rule": _STR,
                    "fix": _STR,
                    "severity": {"type": "string", "enum": ["warn", "critical"]},
                }
            ),
        },
        "soft_delete": {
            "type": _nullable("object"),
            "properties": {"predicate": _STR, "marker_columns": _COLS},
            "required": ["predicate", "marker_columns"],
            "additionalProperties": False,
        },
        "type_notes": {
            "type": "array",
            "items": _obj(
                {
                    "column": _STR,
                    "stored_as": _STR,
                    "means": _STR,
                    "pattern": {"type": _nullable("string")},
                }
            ),
        },
        "pii": {
            "type": "array",
            "items": _obj(
                {
                    "column": _STR,
                    "category": {
                        "type": "string",
                        "enum": [
                            "email",
                            "phone",
                            "name",
                            "address",
                            "gov_id",
                            "card",
                            "iban",
                            "ip",
                            "free_text",
                            "other",
                        ],
                    },
                }
            ),
        },
        "join_candidates": {
            "type": "array",
            "items": _obj({"columns": _COLS, "to_table": _STR, "to_columns": _COLS}),
        },
        "glossary": {
            "type": "array",
            "items": _obj({"term": _STR, "definition": _STR}),
        },
    }
)

DRAFT_SCHEMA: dict[str, Any] = _obj({"tables": {"type": "array", "items": TABLE_DRAFT_SCHEMA}})

_TYPES: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
}


def _type_ok(value: Any, t: str) -> bool:
    if t == "null":
        return value is None
    if t in ("integer", "number") and isinstance(value, bool):
        return False
    return isinstance(value, _TYPES[t])


def validate(value: Any, schema: Mapping[str, Any], path: str = "$") -> list[str]:
    """Return every violation of ``schema`` in ``value`` (empty list = valid)."""
    errors: list[str] = []
    types = schema.get("type")
    if types is not None:
        tlist = [types] if isinstance(types, str) else list(types)
        if not any(_type_ok(value, t) for t in tlist):
            return [f"{path}: expected {'/'.join(tlist)}, got {type(value).__name__}"]
    if value is None:
        return errors
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} is not one of {schema['enum']}")
    if isinstance(value, str) and len(value.strip()) < schema.get("minLength", 0):
        errors.append(f"{path}: empty string")
    if isinstance(value, dict):
        props: Mapping[str, Any] = schema.get("properties", {})
        for key in schema.get("required", ()):
            if key not in value:
                errors.append(f"{path}: missing {key!r}")
        if schema.get("additionalProperties") is False:
            errors += [f"{path}: unexpected key {k!r}" for k in value if k not in props]
        for key, sub in props.items():
            if key in value:
                errors += validate(value[key], sub, f"{path}.{key}")
    if isinstance(value, list):
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: more than {schema['maxItems']} items")
        item = schema.get("items")
        if item is not None:
            for i, v in enumerate(value):
                errors += validate(v, item, f"{path}[{i}]")
    return errors
