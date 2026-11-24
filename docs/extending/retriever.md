# Retriever and embedder

The retriever picks the seed tables for a question (step 1 of [scoping](../guide/scoping.md)).

## Protocol

```py
class Retriever(Protocol):
    def search(self, question: str, k: int) -> list[tuple[ObjectRef, float]]: ...

class Embedder(Protocol):
    @property
    def model_id(self) -> str: ...                 # names the model and version
    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...
```

Return at most `k` `(table ref, score)` pairs, highest score first, ties in a stable order;
only tables that exist in the store. Shipped: `LexicalRetriever(store)` (the default),
`EmbeddingRetriever(store, embedder)` (extra `embeddings`, numpy), and
`HybridRetriever(*retrievers, k_rrf=60)` (reciprocal-rank fusion).

## Worked example: a synonym retriever, fused with the default

```python
import schemaguru as sg
from schemaguru.scope import HybridRetriever, LexicalRetriever


class SynonymRetriever:
    """Maps business words to tables the names do not reveal."""

    def __init__(self, store, synonyms: dict[str, list[str]]) -> None:
        self.store, self.synonyms = store, synonyms

    def search(self, question: str, k: int) -> list[tuple[sg.ObjectRef, float]]:
        hits: dict[sg.ObjectRef, float] = {}
        for word in question.lower().split():
            for name in self.synonyms.get(word.strip("?,."), []):
                ref = sg.ObjectRef.parse(name)
                if self.store.table(ref) is not None:
                    hits[ref] = hits.get(ref, 0.0) + 1.0
        ranked = sorted(hits.items(), key=lambda kv: (-kv[1], kv[0].canonical()))
        return ranked[:k]


guru = sg.Guru.connect("duckdb:///warehouse.duckdb")
synonyms = {"clicks": ["shop.events"], "baskets": ["shop.order_items"]}
guru.retriever = HybridRetriever(LexicalRetriever(guru.store),
                                 SynonymRetriever(guru.store, synonyms))
print([t.table for t in guru.context("clicks and baskets per day").tables])
```

Pass it at construction with `sg.Guru(..., retriever=...)`; assigning `guru.retriever` as
above works too, as long as no view for the same question was cached yet.

## Worked example: an embedder

`EmbeddingRetriever` stores one vector per table document in the store, keyed by
`model_id`, and re-embeds only tables whose document changed. Any model fits behind
`Embedder`; this toy one hashes words into a small vector so the example runs offline:

```python
import hashlib

from schemaguru.scope import EmbeddingRetriever


class HashingEmbedder:
    model_id = "hashing-64-v1"

    def embed(self, texts):
        out = []
        for text in texts:
            v = [0.0] * 64
            for word in text.lower().split():
                v[int(hashlib.sha256(word.encode()).hexdigest(), 16) % 64] += 1.0
            out.append(v)
        return out


emb = EmbeddingRetriever(guru.store, HashingEmbedder())
print(emb.index(), "tables embedded")
print([(r.table, round(s, 3)) for r, s in emb.search("payments for an order", 3)])
guru.close()
```

## Test template

```py
def test_retriever_contract(shop_store):
    r = MyRetriever(shop_store)
    hits = r.search("orders by customer", 3)
    assert len(hits) <= 3
    assert [s for _, s in hits] == sorted((s for _, s in hits), reverse=True)
    assert all(shop_store.table(ref) is not None for ref, _ in hits)
    assert r.search("orders by customer", 3) == hits          # deterministic
    assert r.search("anything", 0) == []
```
