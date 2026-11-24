"""Text helpers (layer 0)."""

from __future__ import annotations

import re

_SPLIT = re.compile(r"[\W_]+|(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def name_tokens(name: str) -> list[str]:
    """Split a snake/camel/space-separated identifier into lowercase words.

    ``customerOrderId`` -> ``customer order id``; ``order_items`` -> ``order items``;
    ``HTTPRequest`` -> ``http request``.
    """
    return [t.lower() for t in _SPLIT.split(name) if t]
