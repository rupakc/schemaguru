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
from schemaguru.model.facts import Fact
from schemaguru.model.joins import JoinRef
from schemaguru.model.refs import ObjectRef
from schemaguru.model.structure import Column, Table

if TYPE_CHECKING:
    import numpy as np

_MEANING_KINDS = ("DESCRIPTION", "USE_CASES", "ENTITY_TYPE", "PITFALL")


class Embedder(Protocol):
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
        model: str = "default",
        batch_size: int = 64,
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.model = model
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
        todo: list[tuple[ObjectRef, str, str]] = []
        for ref in targets:
            doc = table_document(self.store, ref)
            h = stable_hash(["table-doc-v1", doc])
            if stored.get(ref) != h:
                todo.append((ref, h, doc))
        for i in range(0, len(todo), self.batch_size):
            batch = todo[i : i + self.batch_size]
            vecs = self.embedder.embed([d for _, _, d in batch])
            if len(vecs) != len(batch):
                raise ValueError(
                    f"embedder returned {len(vecs)} vectors for {len(batch)} documents"
                )
            self.store.embeddings_put(
                self.model, [(r, h, v) for (r, h, _), v in zip(batch, vecs, strict=True)]
            )
        if todo:
            self._matrix = None
        return len(todo)

    def reload(self) -> None:
        """Drop the in-memory matrix (after another process re-indexed the store)."""
        self._matrix = None

    def _load(self) -> tuple[list[ObjectRef], np.ndarray[Any, Any]]:
        import numpy as np

        if self._matrix is None:
            rows = self.store.embeddings_get(self.model)
            dims = {d for _, _, d, _ in rows}
            dim = max(dims, key=lambda d: (sum(1 for r in rows if r[2] == d), -d)) if dims else 0
            keep = [r for r in rows if r[2] == dim]
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
        q = np.asarray(self.embedder.embed([question])[0], dtype=np.float32)
        norm = float(np.linalg.norm(q))
        if q.shape[0] != mat.shape[1] or norm == 0.0:
            return []
        sims = mat @ (q / norm)
        pos = np.flatnonzero(sims > 0.0)
        if len(pos) > k:  # keep every tie with the k-th score, then order deterministically
            kth = np.partition(sims[pos], len(pos) - k)[len(pos) - k]
            pos = pos[sims[pos] >= kth]
        hits = [(refs[i], float(sims[i])) for i in pos]
        hits.sort(key=lambda kv: (-kv[1], kv[0].canonical()))
        return hits[:k]
