"""Stable content hashing (layer 0)."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any

from schemaguru._util.timeutil import require_aware


def _canon(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, datetime):
        return require_aware(obj).astimezone(timezone.utc).isoformat()
    if isinstance(obj, Enum):
        return obj.value
    raise TypeError(f"not hashable canonically: {type(obj).__name__}")


def canonical_json(obj: object) -> str:
    """Compact, key-sorted JSON; ``Decimal`` -> string, aware datetimes -> UTC ISO, enums -> value.

    Those values come back as strings when decoded (no type tags); every store and export uses
    this one encoder so equal content is always byte-equal.
    """
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_canon
    )


def stable_hash(obj: object) -> str:
    """``sha256:<hex>`` of the canonical JSON of ``obj`` (sorted keys, no whitespace)."""
    text = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=_canon)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
