"""Token counting (spec 12.1 step 4): a protocol, the default chars/4 heuristic, tiktoken."""

from __future__ import annotations

import importlib
import math
from typing import Any, Protocol


class TokenCounter(Protocol):
    def count(self, text: str) -> int: ...


class CharTokenCounter:
    """The default heuristic: ``ceil(len(text) / 4)``. Exact counters are extras."""

    def count(self, text: str) -> int:
        return math.ceil(len(text) / 4)


class TiktokenCounter:
    """Exact token counts with ``tiktoken`` (extra ``schemaguru[tiktoken]``, imported lazily)."""

    def __init__(self, encoding: str = "cl100k_base") -> None:
        try:
            tiktoken = importlib.import_module("tiktoken")
        except ImportError:
            raise ImportError(
                "TiktokenCounter needs tiktoken: pip install 'schemaguru[tiktoken]'"
            ) from None
        self.encoding = encoding
        self._enc: Any = tiktoken.get_encoding(encoding)

    def count(self, text: str) -> int:
        return len(self._enc.encode(text, disallowed_special=()))
