"""Join candidates (spec 8.4): where the joins to measure come from.

In priority order: declared constraints, overlays (human YAML), dbt, name/type heuristics, LLM
drafts. A candidate is never a fact: it becomes a ``JOIN`` fact only after measurement.

Heuristics (spec 8.4.3), bounded to ``max_per_column`` per column and to the table's catalog
unless ``same_catalog_only=False``:

1. a column ``x_id`` / ``xId`` / ``x_fk`` -> a table named ``x`` or ``xs`` (any schema) with a
   single-column primary key, else an ``id`` column, of a compatible type family;
2. a column whose name equals another table's single-column primary key in the same schema.

Compatible families: equal, or both numeric (``integer``/``decimal``). A string key against a
numeric one is a candidate measured with ``cast_to="string"`` (the agent must cast too).
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from schemaguru.model.facts import Provenance
from schemaguru.model.joins import JoinRef
from schemaguru.model.refs import ObjectRef
from schemaguru.model.structure import Column
from schemaguru.store.protocol import Store

_NUMERIC = frozenset({"integer", "decimal"})
_STRINGISH = frozenset({"string"})
_STEM = (
    re.compile(r"^(?P<x>.+?)_(?:id|fk)$", re.IGNORECASE),
    re.compile(r"^(?P<x>[a-z][A-Za-z0-9]*?)(?:Id|ID|Fk|FK)$"),
)
_RANK = {"constraint": 0, "overlay": 1, "dbt": 2, "heuristic": 3, "llm": 4}


def _cast(left: str, right: str) -> tuple[bool, str | None]:
    """``(compatible, cast_to)`` for two type families."""
    if left == right and left != "other":
        return True, None
    if left in _NUMERIC and right in _NUMERIC:
        return True, None
    if (left in _NUMERIC and right in _STRINGISH) or (left in _STRINGISH and right in _NUMERIC):
        return True, "string"
    return False, None


def _stem(name: str) -> str | None:
    for pattern in _STEM:
        m = pattern.match(name)
        if m:
            return m.group("x")
    return None


def _key_column(store: Store, table: ObjectRef) -> Column | None:
    """The table's single-column primary key, else its ``id`` column."""
    cols = store.columns(table)
    by_name = {c.ref.column: c for c in cols}
    for con in store.constraints(table):
        if con.kind == "primary_key" and len(con.columns) == 1 and con.columns[0] in by_name:
            return by_name[con.columns[0]]
    for c in cols:
        if (c.ref.column or "").lower() == "id":
            return c
    return None


def _single_pk(store: Store, table: ObjectRef) -> Column | None:
    cols = {c.ref.column: c for c in store.columns(table)}
    for con in store.constraints(table):
        if con.kind == "primary_key" and len(con.columns) == 1:
            return cols.get(con.columns[0])
    return None


def _join(left: Column, right: Column) -> JoinRef | None:
    ok, cast = _cast(left.family, right.family)
    if not ok or left.ref.table_ref() == right.ref.table_ref():
        return None
    return JoinRef((left.ref,), (right.ref,), cast)


def _families(store: Store, table: ObjectRef) -> dict[str, str]:
    return {c.ref.column or "": c.family for c in store.columns(table)}


def _schemas(
    store: Store, table: ObjectRef, same_catalog_only: bool
) -> list[tuple[str | None, str]]:
    seen: dict[tuple[str | None, str], None] = {}
    if table.schema is not None:
        seen[(table.catalog, table.schema)] = None
    for s in store.iter_schemas():
        if same_catalog_only and s.catalog != table.catalog:
            continue
        seen[(s.catalog, s.name)] = None
    return list(seen)


def _named_tables(
    store: Store, catalog: str | None, schema: str, names: Iterable[str]
) -> list[ObjectRef]:
    found: list[ObjectRef] = []
    for name in names:
        ref, ambiguous = store.resolve(ObjectRef(catalog, schema, name).canonical())
        for r in [ref] if ref is not None else ambiguous:
            if r is not None and r.catalog == catalog and r.schema == schema and r not in found:
                t = store.table(r)
                if t is not None and t.withdrawn_at is None:
                    found.append(r)
    return found


def _heuristics(
    store: Store, table: ObjectRef, max_per_column: int, same_catalog_only: bool
) -> list[JoinRef]:
    cols = store.columns(table)
    if not cols:
        return []
    schemas = _schemas(store, table, same_catalog_only)
    # rule 2 index: single-column PK name -> PK columns of other tables in the same schema
    pk_by_name: dict[str, list[Column]] = {}
    for t in store.iter_tables():
        r = t.ref
        if r == table or r.catalog != table.catalog or r.schema != table.schema:
            continue
        pk = _single_pk(store, r)
        if pk is not None and pk.ref.column is not None:
            pk_by_name.setdefault(pk.ref.column, []).append(pk)
    out: list[JoinRef] = []
    for c in cols:
        name = c.ref.column or ""
        found: list[JoinRef] = []

        def add(j: JoinRef | None, found: list[JoinRef] = found) -> None:
            if j is not None and j not in found and len(found) < max_per_column:
                found.append(j)

        stem = _stem(name)
        if stem:
            for catalog, schema in schemas:
                for target in _named_tables(store, catalog, schema, (stem, stem + "s")):
                    key = _key_column(store, target)
                    if key is not None:
                        add(_join(c, key))
        for pk in pk_by_name.get(name, []):
            add(_join(c, pk))
        out += found
    return out


def join_candidates(
    store: Store, table: ObjectRef, *, max_per_column: int = 5, same_catalog_only: bool = True
) -> list[tuple[JoinRef, str]]:
    """Candidate joins with ``table`` on the child (left) side, labelled by origin
    (``constraint, overlay, dbt, heuristic, llm``) in that priority order, de-duplicated (the
    first origin wins). Reads the store only; never writes a fact."""
    table = table.table_ref()
    if store.table(table) is None:
        return []
    found: list[tuple[JoinRef, str]] = []
    fam = _families(store, table)
    for con in store.constraints(table):
        if con.kind != "foreign_key" or con.ref_table is None or not con.ref_columns:
            continue
        if len(con.ref_columns) != len(con.columns):
            continue
        rfam = _families(store, con.ref_table)
        casts = {
            _cast(fam.get(a, "other"), rfam.get(b, "other"))[1]
            for a, b in zip(con.columns, con.ref_columns, strict=True)
        }
        cast = "string" if "string" in casts else None
        left = tuple(ObjectRef(table.catalog, table.schema, table.table, c) for c in con.columns)
        rt = con.ref_table
        right = tuple(ObjectRef(rt.catalog, rt.schema, rt.table, c) for c in con.ref_columns)
        found.append((JoinRef(left, right, cast), "constraint"))
    for f in store.facts_for(None, ["JOIN"]):
        j = f.subject
        if not isinstance(j, JoinRef) or j.left[0].table_ref() != table:
            continue
        if f.provenance is Provenance.CATALOG:
            label = "constraint"
        elif f.provenance is Provenance.HUMAN:
            label = "overlay"
        elif f.provenance is Provenance.DECLARED:
            label = "dbt" if (f.source or "").startswith("dbt:") else "overlay"
        elif f.provenance is Provenance.DRAFTED:
            label = "llm"
        else:
            continue  # a measured join came from some candidate; it is not a source itself
        found.append((j, label))
    found += [
        (j, "heuristic") for j in _heuristics(store, table, max_per_column, same_catalog_only)
    ]
    found.sort(key=lambda jl: _RANK[jl[1]])  # stable: keeps order within an origin
    seen: set[str] = set()
    out: list[tuple[JoinRef, str]] = []
    for j, label in found:
        key = j.canonical()
        if key not in seen:
            seen.add(key)
            out.append((j, label))
    return out
