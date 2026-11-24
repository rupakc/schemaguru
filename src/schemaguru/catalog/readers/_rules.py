"""Helpers shared by the dialect readers added with the warehouse set: family rule tables,
keyset-paging utilities and name-pair clauses. Local to ``catalog/readers``."""

from __future__ import annotations

import re
from datetime import datetime, timezone

Rules = tuple[tuple["re.Pattern[str]", str], ...]


def compile_rules(pairs: tuple[tuple[str, str], ...]) -> Rules:
    return tuple((re.compile(p, re.IGNORECASE), fam) for p, fam in pairs)


def family_by(rules: Rules, text: str) -> str:
    """First matching rule's family, else ``other`` (never a guess)."""
    t = text.strip()
    for pattern, family in rules:
        if pattern.search(t):
            return family
    return "other"


def utc(value: datetime | None) -> datetime | None:
    """A naive datetime that the SQL already converted to UTC becomes tz-aware; aware ones are
    normalised; ``None`` stays ``None``."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def blank_to_none(text: str | None) -> str | None:
    return None if text is None or text == "" else text
