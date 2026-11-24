"""Seed retrieval: which tables a question is about (spec 12.1 steps 1-2).

Every retriever returns ``(table, score)`` pairs, higher is better, ordered by score then
canonical ref, so the same store and question always give the same list (spec 12.2).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping
from typing import Any, Protocol

from schemaguru._util.text import name_tokens
from schemaguru.model.refs import ObjectRef
from schemaguru.overlays.schema import GLOBAL_SUBJECT
from schemaguru.store.protocol import Store

IDENTIFIER_BOOST = 2.0
GLOSSARY_WEIGHT = 1.0
_GLOSSARY_KINDS = ("GLOSSARY", "METRIC")

# Function words never count as a query term or as a verbatim table name.
STOPWORDS = frozenset(
    [
        "a",
        "about",
        "all",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "been",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "each",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "i",
        "in",
        "into",
        "is",
        "it",
        "its",
        "last",
        "many",
        "me",
        "much",
        "my",
        "of",
        "on",
        "or",
        "our",
        "over",
        "per",
        "show",
        "so",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "this",
        "those",
        "to",
        "us",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "within",
        "without",
        "would",
        "you",
        "your",
    ]
)
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*(?:\.[A-Za-z_][A-Za-z0-9_$]*){0,3}")


class Retriever(Protocol):
    def search(self, question: str, k: int) -> list[tuple[ObjectRef, float]]: ...


def _ranked(scores: Mapping[ObjectRef, float], k: int) -> list[tuple[ObjectRef, float]]:
    order = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0].canonical()))
    return order[: max(k, 0)]


def _stem(word: str) -> str:
    """A light plural fold so 'accounts' matches the glossary term 'account'."""
    if len(word) > 3 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _refs(value: object) -> Iterator[ObjectRef]:
    items = value if isinstance(value, (list, tuple)) else [value]
    for item in items:
        try:
            if isinstance(item, Mapping):
                yield ObjectRef.from_dict(dict(item)).table_ref()
            elif isinstance(item, str):
                yield ObjectRef.parse(item).table_ref()
        except (KeyError, TypeError, ValueError):
            continue  # one bad item costs one item (spec 25.14)


class LexicalRetriever:
    """Store full-text search, plus verbatim identifiers and glossary/metric terms.

    * The store's FTS (BM25 over name tokens, column names, comments, descriptions, use
      cases, pitfalls) ranks candidates; it is asked for ``oversample * k`` hits.
    * A table named verbatim in the question -- canonical (``shop.orders``) or by its bare
      table name (``orders``, case-insensitive) -- has its score multiplied by
      ``identifier_boost``; if the text search missed it, it enters at the top text score.
    * A glossary term or metric name whose words all occur in the question pulls in its
      ``maps_to`` tables with ``GLOSSARY_WEIGHT`` times the top text score added.
    """

    def __init__(
        self,
        store: Store,
        *,
        identifier_boost: float = IDENTIFIER_BOOST,
        oversample: int = 4,
        max_name_matches: int = 16,
    ) -> None:
        self.store = store
        self.identifier_boost = identifier_boost
        self.oversample = max(1, oversample)
        self.max_name_matches = max_name_matches

    def search(self, question: str, k: int) -> list[tuple[ObjectRef, float]]:
        if k <= 0:
            return []
        words = [w for w in dict.fromkeys(name_tokens(question)) if w not in STOPWORDS]
        scores: dict[ObjectRef, float] = {}
        if words:
            scores.update(self.store.search(" ".join(words), k * self.oversample))
        top = max(scores.values(), default=1.0)
        for ref in self._named(question):
            scores[ref] = scores.get(ref, top) * self.identifier_boost
        stems = {_stem(w) for w in words}
        for ref in self._glossary(stems):
            scores[ref] = scores.get(ref, 0.0) + GLOSSARY_WEIGHT * top
        return _ranked(scores, k)

    def _named(self, question: str) -> set[ObjectRef]:
        bare: list[str] = []
        found: set[ObjectRef] = set()
        for ident in dict.fromkeys(_IDENT.findall(question)):
            if "." in ident:
                ref, _ = self.store.resolve(ident)
                if ref is not None:
                    table = self.store.table(ref)
                    if table is not None and table.withdrawn_at is None:
                        found.add(ref.table_ref())
            elif ident.lower() not in STOPWORDS:
                bare.append(ident)
        found.update(self.store.tables_named(bare, limit=self.max_name_matches))
        return found

    def _glossary(self, stems: set[str]) -> list[ObjectRef]:
        if not stems:
            return []
        out: dict[ObjectRef, None] = {}
        live: dict[ObjectRef, bool] = {}
        for fact in self.store.facts_for([GLOBAL_SUBJECT], _GLOSSARY_KINDS):
            value: Mapping[str, Any] = fact.value
            term = value.get("term") or value.get("name")
            if not isinstance(term, str):
                continue
            words = {_stem(w) for w in name_tokens(term)}
            if not words or not words <= stems:
                continue
            for ref in _refs(value.get("maps_to") or ()):
                if ref not in live:
                    table = self.store.table(ref)
                    live[ref] = table is not None and table.withdrawn_at is None
                if live[ref]:
                    out[ref] = None
        return list(out)


class HybridRetriever:
    """Reciprocal-rank fusion: ``score = sum(1 / (k_rrf + rank))`` over the retrievers.

    Each retriever is asked for ``depth`` hits (default ``2 * k``); ranks are 1-based.
    """

    def __init__(self, *retrievers: Retriever, k_rrf: int = 60, depth: int | None = None):
        if not retrievers:
            raise ValueError("HybridRetriever needs at least one retriever")
        if k_rrf <= 0:
            raise ValueError("k_rrf must be positive")
        self.retrievers: tuple[Retriever, ...] = retrievers
        self.k_rrf = k_rrf
        self.depth = depth

    def search(self, question: str, k: int) -> list[tuple[ObjectRef, float]]:
        if k <= 0:
            return []
        depth = self.depth if self.depth is not None else 2 * k
        fused: dict[ObjectRef, float] = {}
        for r in self.retrievers:
            for rank, (ref, _) in enumerate(_dedupe(r.search(question, depth)), start=1):
                fused[ref] = fused.get(ref, 0.0) + 1.0 / (self.k_rrf + rank)
        return _ranked(fused, k)


def _dedupe(hits: Iterable[tuple[ObjectRef, float]]) -> list[tuple[ObjectRef, float]]:
    seen: set[ObjectRef] = set()
    out = []
    for ref, score in hits:
        if ref not in seen:
            seen.add(ref)
            out.append((ref, score))
    return out
