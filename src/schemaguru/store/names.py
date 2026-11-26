"""Relation names read from a definition, resolved against the store (0.2.0 spec 4.4).

A name is matched with the store's own rule (:meth:`Store.resolve`: exact, then a unique
case-insensitive match). An ambiguous name, a name the store does not hold and a withdrawn
table are left out of the result, so the parser lists them as ``unresolved`` (never a guess).
"""

from __future__ import annotations

from collections.abc import Sequence

from schemaguru.model.lineage import RawName, ResolvedTable
from schemaguru.model.refs import ObjectRef
from schemaguru.store.protocol import Store


def _candidates(name: RawName, default: ObjectRef) -> list[ObjectRef]:
    """The refs to try for ``name``, in order (see :func:`resolve_relation_names`)."""
    catalog, schema, table = name
    parts: list[tuple[str | None, str | None, str]]
    if catalog is not None:
        parts = [(catalog, schema, table)] if schema is not None else []
    elif schema is not None:
        parts = [(default.catalog, schema, table), (None, schema, table)]
    else:
        parts = [(default.catalog, default.schema, table), (None, default.schema, table)]
    out: list[ObjectRef] = []
    for c, s, t in parts:
        try:
            ref = ObjectRef(c, s, t)
        except ValueError:
            continue
        if ref not in out:
            out.append(ref)
    return out


def _resolve_one(store: Store, ref: ObjectRef) -> tuple[ResolvedTable | None, bool]:
    """``(table, ambiguous)`` for one candidate."""
    text = ref.canonical()
    found, cands = store.resolve(text)
    if found is not None and found.column is None:
        stored = store.table(found)
        if stored is None or stored.withdrawn_at is not None:
            return None, False
        cols = sorted(store.columns(found), key=lambda c: c.ordinal)
        return (
            ResolvedTable(found.canonical(), tuple(c.ref.column or "" for c in cols)),
            False,
        )
    # ``Store.resolve`` may also read the text as ``table.column``; only an ambiguity between
    # tables spelled like the candidate counts.
    folded = text.lower()
    ambiguous = any(c.column is None and c.canonical().lower() == folded for c in cands)
    return None, ambiguous


def resolve_relation_names(
    store: Store, names: Sequence[RawName], default: ObjectRef
) -> dict[RawName, ResolvedTable]:
    """The stored relation each name refers to; ``default`` is the definition's own ref.

    A three-part name is tried as written; a two-part ``s.n`` as ``(default.catalog, s, n)``,
    then ``(None, s, n)``; a bare ``n`` as ``(default.catalog, default.schema, n)``, then
    ``(None, default.schema, n)``. The first candidate that resolves wins; an ambiguous
    candidate ends the search for that name (it stays unresolved).
    """
    out: dict[RawName, ResolvedTable] = {}
    for name in names:
        if name in out:
            continue
        for ref in _candidates(name, default):
            table, ambiguous = _resolve_one(store, ref)
            if table is not None:
                out[name] = table
                break
            if ambiguous:
                break
    return out
