"""Token counting (spec 12.1 step 4): a protocol and the default chars/4 heuristic."""

from __future__ import annotations

import math
from typing import Protocol


class TokenCounter(Protocol):
    def count(self, text: str) -> int: ...


class CharTokenCounter:
    """The default heuristic: ``ceil(len(text) / 4)``. Exact counters are extras."""

    def count(self, text: str) -> int:
        return math.ceil(len(text) / 4)
