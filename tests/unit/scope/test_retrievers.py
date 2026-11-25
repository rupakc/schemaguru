from __future__ import annotations

import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from schemaguru.model.facts import Fact, Provenance, Status
from schemaguru.model.kinds import FactKind
from schemaguru.model.refs import ObjectRef
from schemaguru.model.structure import Column, Table
from schemaguru.overlays.schema import GLOBAL_SUBJECT
from schemaguru.scope.retrievers import HybridRetriever, LexicalRetriever, Retriever
from schemaguru.store.sqlite import SQLiteStore


def t(name: str, schema: str = "shop") -> ObjectRef:
    return ObjectRef(None, schema, name)


def drafted(kind: Any, subject: ObjectRef, value: dict[str, Any]) -> Fact:
    return Fact.make(kind, subject, value, Provenance.DRAFTED, Status.UNVERIFIED)


def human(kind: Any, subject: ObjectRef, value: dict[str, Any]) -> Fact:
    return Fact.make(kind, subject, value, Provenance.HUMAN, Status.UNVERIFIED)


@pytest.fixture
def shop(shop_store: SQLiteStore) -> SQLiteStore:
    shop_store.put_facts(
        [
            drafted(
                FactKind.USE_CASES,
                t("orders"),
                {"questions": ["revenue by month", "average basket size", "order volume"]},
            ),
            drafted(FactKind.USE_CASES, t("customers"), {"questions": ["customer segments"]}),
            drafted(FactKind.USE_CASES, t("events"), {"questions": ["funnel conversion"]}),
            drafted(FactKind.DESCRIPTION, t("payments"), {"text": "one row per card charge"}),
        ]
    )
    return shop_store


def refs(hits: Sequence[tuple[ObjectRef, float]]) -> list[str]:
    return [r.table for r, _ in hits]


def test_lexical_finds_orders_for_revenue_question(shop: SQLiteStore) -> None:
    r: Retriever = LexicalRetriever(shop)
    hits = r.search("What was our revenue last month?", k=3)
    assert refs(hits)[0] == "orders"
    assert all(s > 0 for _, s in hits)
    # deterministic: same input, same output
    assert r.search("What was our revenue last month?", k=3) == hits
    assert r.search("revenue", k=0) == []
    assert r.search("the of and", k=5) == []  # nothing but stopwords: no guess


def test_identifier_boost(shop: SQLiteStore) -> None:
    r = LexicalRetriever(shop)
    q = "funnel conversion per segment"  # events (use case) beats customers (column 'segment')
    plain = dict(r.search(q, k=5))
    assert refs(r.search(q, k=5))[0] == "events"
    # naming a table verbatim, bare or qualified, doubles its score
    bare = dict(r.search(q + " in customers", k=5))
    assert bare[t("customers")] == pytest.approx(
        2.0
        * dict(LexicalRetriever(shop, identifier_boost=1.0).search(q + " in customers", 5))[
            t("customers")
        ]
    )
    assert refs(r.search(q + " from shop.customers", k=5))[0] == "customers"
    assert t("customers") in plain
    # a verbatim name the text search would not surface is still found (name, not tokens)
    hit = dict(r.search("rows in ref_parent", k=3))
    assert t("ref_parent") in hit
    # case-insensitive bare names; a withdrawn table is never boosted in
    assert refs(r.search("REF_CHILD", k=1)) == ["ref_child"]


def test_identifier_boost_skips_withdrawn(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    with SQLiteStore(tmp_path / "w.db") as s:
        s.upsert_tables([Table(t("ledger"), "table"), Table(t("journal"), "table")])
        s.mark_withdrawn([t("ledger")], datetime(2026, 11, 24, tzinfo=timezone.utc))
        r = LexicalRetriever(s)
        assert r.search("ledger", k=5) == []
        assert r.search("shop.ledger journal", k=5)[0][0] == t("journal")
        assert [x for x, _ in r.search("shop.ledger journal", k=5)] == [t("journal")]


def test_glossary_maps_tables(shop: SQLiteStore) -> None:
    shop.put_facts(
        [
            human(
                FactKind.GLOSSARY,
                GLOBAL_SUBJECT,
                {
                    "term": "churned account",
                    "definition": "no event in 90 days",
                    "maps_to": [t("accounts").to_dict(), t("events").to_dict()],
                },
            ),
            human(
                FactKind.METRIC,
                GLOBAL_SUBJECT,
                {"name": "GMV", "definition": "gross value", "maps_to": ["shop.order_items"]},
            ),
            human(  # mapped to a table that does not exist: ignored, never raises
                FactKind.GLOSSARY,
                GLOBAL_SUBJECT,
                {"term": "ghost", "definition": "x", "maps_to": ["shop.nope", 7]},
            ),
        ]
    )
    r = LexicalRetriever(shop)
    got = refs(r.search("how many churned accounts last quarter", k=4))
    assert set(got[:2]) == {"accounts", "events"}
    gmv = refs(r.search("gmv by week", k=3))
    assert gmv[0] == "order_items"  # no text hit at all: the metric alone brings it in
    assert r.search("ghost", k=3) == []
    # partial term ("churned" alone) does not fire the glossary entry: the store's own text
    # match on the indexed term (spec 7.2) is all there is, no glossary boost on top
    assert r.search("churned", k=4) == shop.search("churned", 4 * r.oversample)[:4]


class Fixed:
    def __init__(self, names: Sequence[str]) -> None:
        self.names = names

    def search(self, question: str, k: int) -> list[tuple[ObjectRef, float]]:
        return [(t(n), 100.0 - i) for i, n in enumerate(self.names[:k])]


def test_rrf_fusion() -> None:
    a = Fixed(["orders", "customers", "payments"])
    b = Fixed(["payments", "orders", "events"])
    h: Retriever = HybridRetriever(a, b, k_rrf=60)
    got = h.search("q", k=4)
    score = dict(got)
    assert score[t("orders")] == pytest.approx(1 / 61 + 1 / 62)
    assert score[t("payments")] == pytest.approx(1 / 63 + 1 / 61)
    assert score[t("events")] == pytest.approx(1 / 63)
    assert refs(got) == ["orders", "payments", "customers", "events"]
    # ties are broken by canonical ref, so fusion is deterministic
    tie = HybridRetriever(Fixed(["b"]), Fixed(["a"])).search("q", k=2)
    assert refs(tie) == ["a", "b"]
    assert h.search("q", k=0) == []
    with pytest.raises(ValueError):
        HybridRetriever()
    with pytest.raises(ValueError):
        HybridRetriever(a, k_rrf=0)


# ----- embeddings ------------------------------------------------------------------------


class BagEmbedder:
    """Deterministic toy embedder: counts of a fixed vocabulary."""

    VOCAB = ("revenue", "order", "customer", "segment", "event", "funnel", "payment", "card")

    def __init__(self, model_id: str = "bag-v1", extra_dims: int = 0) -> None:
        self.calls = 0
        self.model_id = model_id
        self.extra_dims = extra_dims

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        out = []
        for text in texts:
            low = text.lower()
            out.append([float(low.count(w)) for w in self.VOCAB] + [0.0] * self.extra_dims)
        return out


def test_embedding_retriever(shop: SQLiteStore) -> None:
    pytest.importorskip("numpy")
    from schemaguru.scope.embeddings import EmbeddingRetriever, table_document

    emb = BagEmbedder()
    r = EmbeddingRetriever(shop, emb, model="bag", batch_size=4)
    assert r.search("revenue", k=3) == []  # nothing indexed yet
    n = r.index()
    assert n == len(list(shop.iter_tables()))
    assert r.index() == 0  # unchanged documents are not re-embedded
    calls = emb.calls
    got = r.search("card payment", k=2)
    assert got[0][0] == t("payments")
    assert math.isclose(got[0][1], max(s for _, s in got))
    assert -1.0 <= got[-1][1] <= 1.0
    assert emb.calls == calls + 1
    assert "revenue by month" in table_document(shop, t("orders"))
    # a changed document is re-embedded on the next index()
    shop.put_facts([drafted(FactKind.DESCRIPTION, t("sessions"), {"text": "funnel funnel"})])
    assert r.index() == 1
    assert r.search("funnel", k=1)[0][0] in {t("sessions"), t("events")}
    # hybrid over lexical + embeddings
    h = HybridRetriever(LexicalRetriever(shop), r)
    assert refs(h.search("card payment", k=2))[0] == "payments"
    assert r.search("zzz", k=3) == []  # zero query vector: nothing is similar


def test_embedding_retriever_explicit_refs_and_bad_embedder(shop: SQLiteStore) -> None:
    pytest.importorskip("numpy")
    from schemaguru.scope.embeddings import EmbeddingRetriever

    class Short:
        model_id = "short"

        def embed(self, texts: Sequence[str]) -> list[list[float]]:
            return [[1.0] for _ in texts[:-1]]

    with pytest.raises(ValueError, match="returned 1 vectors for 2"):
        EmbeddingRetriever(shop, Short()).index([t("orders"), t("customers")])
    r = EmbeddingRetriever(shop, BagEmbedder())
    assert r.index([t("orders"), t("does_not_exist")]) == 1
    assert [x for x, _ in r.search("order", k=5)] == [t("orders")]


def test_table_document_includes_column_meaning(tmp_path: Path) -> None:
    from schemaguru.scope.embeddings import table_document

    with SQLiteStore(tmp_path / "d.db") as s:
        s.upsert_tables([Table(t("orders"), "table", comment="All orders")])
        s.upsert_columns(
            t("orders"),
            [Column(ObjectRef(None, "shop", "orders", "grossAmt"), 1, "INT", "integer", None, "c")],
        )
        s.put_facts(
            [
                drafted(
                    FactKind.DESCRIPTION,
                    ObjectRef(None, "shop", "orders", "grossAmt"),
                    {"text": "gross amount in cents"},
                )
            ]
        )
        doc = table_document(s, t("orders"))
        assert "gross amt" in doc and "gross amount in cents" in doc and "All orders" in doc
        assert table_document(s, t("missing")) == ""


def test_glossary_tolerates_bad_entries(shop: SQLiteStore) -> None:
    shop.put_facts(
        [
            human(FactKind.METRIC, GLOBAL_SUBJECT, {"definition": "no name", "maps_to": []}),
            human(
                FactKind.GLOSSARY,
                GLOBAL_SUBJECT,
                {
                    "term": "categories",
                    "definition": "x",
                    "maps_to": ["a..b", {"no_table": 1}, "shop.products"],
                },
            ),
        ]
    )
    # 'categories' folds to 'category' on both sides; the two malformed refs are skipped
    assert refs(LexicalRetriever(shop).search("category mix", k=1)) == ["products"]


def test_embedding_reload_and_nonpositive_k(shop: SQLiteStore) -> None:
    pytest.importorskip("numpy")
    from schemaguru.scope.embeddings import EmbeddingRetriever

    r = EmbeddingRetriever(shop, BagEmbedder())
    r.index()
    first = r.search("order", k=2)
    r.reload()
    assert r.search("order", k=2) == first
    assert r.search("order", k=0) == []


def test_embedding_dimension_swap_under_same_id_is_never_silent(shop: SQLiteStore) -> None:
    pytest.importorskip("numpy")
    from schemaguru.errors import SchemaGuruError
    from schemaguru.scope.embeddings import EmbeddingRetriever

    n = EmbeddingRetriever(shop, BagEmbedder("m")).index()
    swapped = EmbeddingRetriever(shop, BagEmbedder("m", extra_dims=1))
    # searching the old 8-dim index with a 9-dim question raises, never returns []
    with pytest.raises(SchemaGuruError, match="dimension"):
        swapped.search("card payment", k=2)
    # the dimension is part of the document hash: index() re-embeds everything
    assert swapped.index() == n
    assert swapped.search("card payment", k=1)[0][0] == t("payments")


def test_embedding_model_id_swap_same_dimension(shop: SQLiteStore) -> None:
    pytest.importorskip("numpy")
    from schemaguru.scope.embeddings import EmbeddingRetriever

    n = EmbeddingRetriever(shop, BagEmbedder("a")).index()
    b = EmbeddingRetriever(shop, BagEmbedder("b"))
    assert b.model == "b"
    assert b.index() == n  # same dimension, different id: vectors are not reused
    assert EmbeddingRetriever(shop, BagEmbedder("b")).index() == 0
    # an explicit model name overrides the embedder's id
    assert EmbeddingRetriever(shop, BagEmbedder("b"), model="pinned").model == "pinned"


def test_embedding_model_id_required(shop: SQLiteStore) -> None:
    from schemaguru.scope.embeddings import EmbeddingRetriever

    class Anonymous:
        def embed(self, texts: Sequence[str]) -> list[list[float]]:
            return [[1.0] for _ in texts]

    with pytest.raises(ValueError, match="model"):
        EmbeddingRetriever(shop, Anonymous())  # type: ignore[arg-type]


def test_dotted_identifier_that_does_not_resolve_still_boosts(shop: SQLiteStore) -> None:
    r = LexicalRetriever(shop)
    # 'orders.order_id' is not a stored ref form the store resolves as a table; its parts are
    # still verbatim names, so orders is boosted over order_items
    assert refs(r.search("sum orders.order_id", k=2))[0] == "orders"
    assert refs(r.search("count nosuch.order_items", k=1)) == ["order_items"]


def test_last_is_not_a_stopword(tmp_path: Path) -> None:
    with SQLiteStore(tmp_path / "l.db") as s:
        s.upsert_tables([Table(t("last_login"), "table"), Table(t("first_login"), "table")])
        assert refs(LexicalRetriever(s).search("last login", k=1)) == ["last_login"]


def test_embedding_mixed_dimensions_raise(shop: SQLiteStore) -> None:
    pytest.importorskip("numpy")
    from schemaguru.errors import SchemaGuruError
    from schemaguru.scope.embeddings import EmbeddingRetriever

    shop.embeddings_put("mix", [(t("orders"), "h", [1.0, 0.0]), (t("customers"), "h", [1.0])])
    with pytest.raises(SchemaGuruError, match="mixes vector dimensions"):
        EmbeddingRetriever(shop, BagEmbedder("mix")).search("order", k=1)
