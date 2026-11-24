"""View fingerprints (spec 13.4): hashes a host's CI compares to detect prompt drift."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from schemaguru._util.hashing import stable_hash
from schemaguru.render.view import ContextView


class ContextSource(Protocol):
    """Anything that renders a view for a question (the ``Guru`` façade does)."""

    def context(self, question: str) -> ContextView: ...


def fingerprint(view: ContextView) -> str:
    """``sha256:<hex>`` of the view's text; equal text, equal fingerprint."""
    return stable_hash(view.text)


def fingerprint_views(guru_like: ContextSource, questions: Iterable[str]) -> dict[str, str]:
    """Render each question and return ``{question: fingerprint}`` in the given order."""
    return {q: fingerprint(guru_like.context(q)) for q in questions}
