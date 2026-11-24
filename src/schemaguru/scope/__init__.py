"""Scoping and retrieval: which slice of the warehouse an agent sees (spec 12)."""

from schemaguru.scope.budget import (
    LITE_MAX_COLUMNS,
    DetailLevel,
    TableRenderer,
    fit,
    lite_columns,
)
from schemaguru.scope.embeddings import Embedder, EmbeddingRetriever, table_document
from schemaguru.scope.expand import expand
from schemaguru.scope.pipeline import Scope, build_scope
from schemaguru.scope.retrievers import (
    IDENTIFIER_BOOST,
    HybridRetriever,
    LexicalRetriever,
    Retriever,
)
from schemaguru.scope.tokens import CharTokenCounter, TokenCounter

__all__ = [
    "IDENTIFIER_BOOST",
    "LITE_MAX_COLUMNS",
    "CharTokenCounter",
    "DetailLevel",
    "Embedder",
    "EmbeddingRetriever",
    "HybridRetriever",
    "LexicalRetriever",
    "Retriever",
    "Scope",
    "TableRenderer",
    "TokenCounter",
    "build_scope",
    "expand",
    "fit",
    "lite_columns",
    "table_document",
]
