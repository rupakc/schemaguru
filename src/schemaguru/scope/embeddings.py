"""Optional embedding retrieval (extra ``embeddings``; spec 12.1 step 1).

Vectors live in the store's ``table_embeddings`` side table, one per ``(model, table)``,
tagged with the hash of the table document they were computed from, so :meth:`index` only
re-embeds tables whose document changed. ``numpy`` is imported lazily, on search.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from typing import TYPE_CHECKING, Any, Protocol

from schemaguru._util.hashing import stable_hash
from schemaguru._util.text import name_tokens
from schemaguru.errors import SchemaGuruError
from schemaguru.model.facts import Fact
from schemaguru.model.joins import JoinRef
from schemaguru.model.refs import ObjectRef
from schemaguru.model.structure import Column, Table

if TYPE_CHECKING:
    import numpy as np

_MEANING_KINDS = ("DESCRIPTION", "USE_CASES", "ENTITY_TYPE", "PITFALL")


class Embedder(Protocol):
    """``model_id`` names the model (and version) that produced the vectors.

    Vectors from different ids are never mixed: stored vectors are keyed by it.
    """

    @property
    def model_id(self) -> str: ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class VectorStore(Protocol):
    """The store surface the embedding retriever needs (``SQLiteStore`` provides it)."""

    def table(self, ref: ObjectRef) -> Table | None: ...

    def iter_tables(self, *, include_withdrawn: bool = False) -> Iterator[Table]: ...

    def columns(self, ref: ObjectRef) -> list[Column]: ...

    def facts_for(
        self,
        subjects: Iterable[ObjectRef | JoinRef] | None,
        kinds: Iterable[str] | None,
    ) -> list[Fact]: ...

    def embeddings_put(
        self, model: str, rows: Iterable[tuple[ObjectRef, str, Sequence[float]]]
    ) -> None: ...

    def embeddings_get(self, model: str) -> list[tuple[ObjectRef, str, int, bytes]]: ...

    def embeddings_hashes(self, model: str) -> dict[ObjectRef, str]: ...


def _strings(obj: Any) -> Iterator[str]:
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _strings(v)


def table_document(store: VectorStore, ref: ObjectRef) -> str:
    """The text embedded for one table: names, comments and meaning facts (table and columns).

    ``""`` for an unknown table. Deterministic for a given store state.
    """
    table = store.table(ref)
    if table is None:
        return ""
    t = table.ref
    cols = store.columns(t)
    lines = [" ".join([*name_tokens(t.schema or ""), *name_tokens(t.table)])]
    if table.comment:
        lines.append(table.comment)
    for c in cols:
        lines.append(
            " ".join(name_tokens(c.ref.column or "")) + (f": {c.comment}" if c.comment else "")
        )
    subjects: list[ObjectRef | JoinRef] = [t, *(c.ref for c in cols)]
    for f in store.facts_for(subjects, _MEANING_KINDS):
        lines.extend(s for s in _strings(f.value) if s)
    return "\n".join(lines)


class EmbeddingRetriever:
    """Cosine similarity between the question's vector and every indexed table's vector."""

    def __init__(
        self,
        store: VectorStore,
        embedder: Embedder,
        *,
        model: str | None = None,
        batch_size: int = 64,
    ) -> None:
        name = model if model is not None else getattr(embedder, "model_id", None)
        if not isinstance(name, str) or not name:
            raise ValueError(
                "EmbeddingRetriever needs a model name: give the embedder a 'model_id' "
                "or pass model=..."
            )
        self.store = store
        self.embedder = embedder
        self.model = name
        self.batch_size = max(1, batch_size)
        self._matrix: tuple[list[ObjectRef], np.ndarray[Any, Any]] | None = None

    def index(self, refs: Iterable[ObjectRef] | None = None) -> int:
        """Embed live tables (all, or ``refs``) whose document changed; returns how many."""
        stored = self.store.embeddings_hashes(self.model)
        if refs is None:
            targets = [t.ref for t in self.store.iter_tables()]
        else:
            targets = []
            for r in dict.fromkeys(x.table_ref() for x in refs):
                t = self.store.table(r)
                if t is not None and t.withdrawn_at is None:
                    targets.append(t.ref)
        # The vector dimension is part of each document hash, so an embedder that changed
        # dimension under the same model name re-embeds everything. Probe it only when there
        # is something stored to compare with.
        dim = len(self._embed(["dimension probe"])[0]) if stored else None
        todo: list[tuple[ObjectRef, str]] = []
        for ref in targets:
            doc = table_document(self.store, ref)
            if dim is None or stored.get(ref) != self._hash(doc, dim):
                todo.append((ref, doc))
        for i in range(0, len(todo), self.batch_size):
            batch = todo[i : i + self.batch_size]
            vecs = self._embed([d for _, d in batch])
            self.store.embeddings_put(
                self.model,
                [(r, self._hash(d, len(v)), v) for (r, d), v in zip(batch, vecs, strict=True)],
            )
        if todo:
            self._matrix = None
        return len(todo)

    def _hash(self, doc: str, dim: int) -> str:
        return stable_hash(["table-doc-v2", self.model, dim, doc])

    def _embed(self, texts: list[str]) -> list[list[float]]:
        vecs = self.embedder.embed(texts)
        if len(vecs) != len(texts):
            raise ValueError(f"embedder returned {len(vecs)} vectors for {len(texts)} documents")
        return vecs

    def reload(self) -> None:
        """Drop the in-memory matrix (after another process re-indexed the store)."""
        self._matrix = None

    def _load(self) -> tuple[list[ObjectRef], np.ndarray[Any, Any]]:
        import numpy as np

        if self._matrix is None:
            rows = self.store.embeddings_get(self.model)
            dims = sorted({d for _, _, d, _ in rows})
            if len(dims) > 1:
                raise SchemaGuruError(
                    f"embedding index {self.model!r} mixes vector dimensions {dims}; "
                    "run index() with the current embedder"
                )
            dim = dims[0] if dims else 0
            keep = rows
            refs = [r for r, _, _, _ in keep]
            mat = np.zeros((len(keep), dim), dtype=np.float32)
            for i, (_, _, _, blob) in enumerate(keep):
                mat[i] = np.frombuffer(blob, dtype="<f4")
            norms = np.linalg.norm(mat, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            self._matrix = (refs, mat / norms)
        return self._matrix

    def search(self, question: str, k: int) -> list[tuple[ObjectRef, float]]:
        import numpy as np

        if k <= 0:
            return []
        refs, mat = self._load()
        if not refs:
            return []
        q = np.asarray(self._embed([question])[0], dtype=np.float32)
        if q.shape[0] != mat.shape[1]:
            raise SchemaGuruError(
                f"embedder {self.model!r} returned a {q.shape[0]}-dimension question vector but "
                f"the stored index has dimension {mat.shape[1]}; run index() to re-embed, or give "
                "a different model its own model_id"
            )
        norm = float(np.linalg.norm(q))
        if norm == 0.0:
            return []
        sims = mat @ (q / norm)
        pos = np.flatnonzero(sims > 0.0)
        if len(pos) > k:  # keep every tie with the k-th score, then order deterministically
            kth = np.partition(sims[pos], len(pos) - k)[len(pos) - k]
            pos = pos[sims[pos] >= kth]
        hits = [(refs[i], float(sims[i])) for i in pos]
        hits.sort(key=lambda kv: (-kv[1], kv[0].canonical()))
        return hits[:k]
