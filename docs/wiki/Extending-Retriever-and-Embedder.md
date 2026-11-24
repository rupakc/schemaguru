# Extending: Retriever and Embedder

> A `Retriever` picks the seed tables for a question; an `Embedder` turns text into vectors for the embedding retriever.
> Any object with `search(question, k) -> [(table ref, score), ...]` is a retriever, and any object with a `model_id` and `embed(texts)` is an embedder.
> Combine retrievers with `HybridRetriever` (reciprocal-rank fusion) and pass the result to the Guru.

## The protocols

```python
class Retriever(Protocol):
    def search(self, question: str, k: int) -> list[tuple[ObjectRef, float]]: ...

class Embedder(Protocol):
    @property
    def model_id(self) -> str: ...                       # names model + version
    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...
```

A retriever returns table refs, higher score better, ordered by score and then by canonical
name (determinism: the same store and question must give the same list). Search inside the
store or an index; never scan every table in Python, which does not scale.

## Worked example: an embedder

```python
class HashingEmbedder:
    """A toy embedder (bag of hashed words); replace with a real model client."""

    model_id = "hashing-v1"          # vectors from different ids are never mixed

    def embed(self, texts):
        out = []
        for text in texts:
            v = [0.0] * 64
            for w in text.lower().split():
                v[int(hashlib.sha256(w.encode()).hexdigest(), 16) % 64] += 1.0
            out.append(v)
        return out
```

```python
from schemaguru.scope import EmbeddingRetriever, HybridRetriever, LexicalRetriever

emb = EmbeddingRetriever(guru.store, HashingEmbedder())    # extra: schemaguru[embeddings] (numpy)
emb.index()                                                # embeds changed table documents; returns a count
guru.retriever = HybridRetriever(LexicalRetriever(guru.store), emb)
guru.retriever.search("customer segment", 3)
```

Real output on the measured shop fixture (2026-11-24): `emb.index()` returned `14`, and the search
returned `customers` (0.0328), `orders` (0.0320), `events` (0.0161). Vectors are stored per
`(model_id, table)` with a hash of the table document, so `index()` re-embeds only tables whose
document changed, and vectors from different `model_id`s are never mixed. Assign the retriever
before the first `context()` call (cached views are not invalidated by the assignment), or pass
`retriever=` to `sg.Guru(...)`.

## Test template

```python
def test_retriever_is_deterministic(guru):
    r = HybridRetriever(LexicalRetriever(guru.store), emb)
    assert r.search("customer segment", 5) == r.search("customer segment", 5)

def test_negative_control_empty_question_finds_nothing_useful(guru):
    assert all(score >= 0 for _, score in emb.search("", 3))
```

## See also

- [[Scoping]]
- [[Extending-TokenCounter]]
- [[Store]]
- [[Configuration]]
