"""Stable content hashing (layer 0)."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any

from schemaguru._util.timeutil import require_aware


def _canon(obj: Any) -> Any:
    """The JSON value of a non-JSON value, shared by :func:`canonical_json` and
    :func:`stable_hash`, so a stored value hashes exactly as the value it was written from."""
    if isinstance(obj, Decimal):
        # its exact text, as in 0.1.0: Decimal("1.0") and Decimal("1.00") stay distinct values,
        # so no fact id differs from the one 0.1.0 computed
        return str(obj)
    if isinstance(obj, datetime):
        return require_aware(obj).astimezone(timezone.utc).isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, Enum):
        return obj.value
    raise TypeError(f"not hashable canonically: {type(obj).__name__}")


def _key_text(key: object) -> str:
    """A dict key as JSON writes it (``1`` -> ``"1"``, ``None`` -> ``"null"``)."""
    if isinstance(key, str):
        return key
    if key is None or isinstance(key, (bool, int, float)):
        return json.dumps(key)
    text = _canon(key)
    return text if isinstance(text, str) else json.dumps(text)


def _text_keys(obj: Any) -> Any:
    """``obj`` with every dict keyed by its keys' JSON text (``{1: .., "a": ..}`` ->
    ``{"1": .., "a": ..}``), so keys of mixed types sort instead of raising ``TypeError``."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            text = _key_text(k)
            if text in out:
                raise ValueError(f"two keys both encode as {text!r}")
            out[text] = _text_keys(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [_text_keys(v) for v in obj]
    return obj


def _dumps(obj: object, *, ensure_ascii: bool) -> str:
    kw: dict[str, Any] = {"sort_keys": True, "separators": (",", ":"), "default": _canon}
    try:
        return json.dumps(obj, ensure_ascii=ensure_ascii, **kw)
    except TypeError:
        # keys that cannot be sorted together: sort them as their JSON text. Only content that
        # used to raise takes this path, so no id computed before it existed changes (a dict of
        # all-integer keys still sorts numerically above). An unencodable value raises again.
        return json.dumps(_text_keys(obj), ensure_ascii=ensure_ascii, **kw)


_INT_TEXT = re.compile(r"-?(0|[1-9][0-9]*)\Z")


def _int_keys(obj: Any) -> Any:
    """``obj`` with every dict whose keys are all integer text keyed by ``int`` again."""
    if isinstance(obj, dict):
        items = {k: _int_keys(v) for k, v in obj.items()}
        if items and all(isinstance(k, str) and _INT_TEXT.match(k) for k in items):
            return {int(k): v for k, v in items.items()}
        return items
    if isinstance(obj, (list, tuple)):
        return [_int_keys(v) for v in obj]
    return obj


def stable_hash_int_keys(obj: object) -> str:
    """:func:`stable_hash` of ``obj`` read back from JSON with integer-text keys made ``int``:
    the id 0.1.0 (and this release) gave a value whose dict had integer keys (``{2: .., 10:
    ..}`` sorts numerically, its stored text ``{"10": .., "2": ..}`` does not)."""
    return stable_hash(_int_keys(obj))


def canonical_json(obj: object) -> str:
    """Compact, key-sorted JSON: the one encoder of every store and export.

    ``Decimal`` -> its exact text (``Decimal("12.50")`` -> ``"12.50"``), aware datetimes ->
    UTC ISO, dates -> ISO, enums -> value; keys of mixed types sort as their JSON text. Those
    values come back as strings when decoded (no type tags) and hash as they were written:
    ``stable_hash(json.loads(canonical_json(x))) == stable_hash(x)`` for such values.
    """
    return _dumps(obj, ensure_ascii=False)


def stable_hash(obj: object) -> str:
    """``sha256:<hex>`` of the canonical JSON of ``obj`` (sorted keys, no whitespace).

    It keeps its own encoder call rather than :func:`canonical_json`: ids are hashed over
    ASCII-escaped text (``ensure_ascii``, as in 0.1.0), and switching encoders would change every
    id that holds a non-ASCII character. Both share ``_canon`` and the key rule, so a stored
    value (decoded from :func:`canonical_json`) hashes like the value it was written from.
    """
    text = _dumps(obj, ensure_ascii=True)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
