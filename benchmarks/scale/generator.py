"""Deterministic synthetic catalog for the scale benchmark (0.2.0 spec 6).

Everything is a pure function of ``(shape, table index)``, so a reader serves any page in
O(page) memory and two readers with one seed list identical catalogs. Table ``i`` lives in
schema ``i // tables_per_schema``. Every ``period``-th table (``period = 1 / view_share``) is a
view; a view's upstream is the two tables just before it (level 1) or, at level ``L > 1``, the
previous view (level ``L - 1``) plus the table before it, so chains are acyclic and at most
``max_lineage_depth`` views deep. Non-view tables carry ``fks_per_table`` foreign keys to
lower-numbered tables.

The join facts from :func:`synthetic_join_facts` are SYNTHETIC: their match rates are made up
by a hash, not measured on any data. A view's declared columns are not derived from its
definition (the definition selects a few columns of its upstreams).
"""

from __future__ import annotations

import random
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from schemaguru.catalog.protocol import Page, ReaderCapabilities
from schemaguru.model.facts import Fact, Provenance, Status
from schemaguru.model.joins import JOIN_VALUE_KEYS, JoinRef
from schemaguru.model.kinds import FactKind
from schemaguru.model.refs import ObjectRef
from schemaguru.model.structure import Column, Constraint, Schema, Table

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
PAGE_SIZE = 1000
_MASK = (1 << 64) - 1

ENTITIES = (
    "orders", "customers", "products", "payments", "shipments", "refunds", "carts", "invoices",
    "suppliers", "reviews", "coupons", "returns", "warehouses", "categories", "addresses",
    "inventory",
)  # fmt: skip
ATTRIBUTES = (
    "amount", "status", "created", "updated", "quantity", "price", "currency", "channel",
    "region", "note", "weight", "discount",
)  # fmt: skip
_FAMILIES = ("string", "decimal", "timestamp", "integer", "string", "boolean")
_NATIVE = {
    "string": "TEXT", "decimal": "NUMERIC", "timestamp": "TIMESTAMP", "integer": "BIGINT",
    "boolean": "BOOLEAN",
}  # fmt: skip


@dataclass(frozen=True)
class ScaleShape:
    tables: int = 500_000
    columns_per_table: int = 50
    tables_per_schema: int = 5_000
    view_share: float = 0.10
    fks_per_table: int = 2
    comment_share: float = 0.5
    max_lineage_depth: int = 6
    seed: int = 20261125


DEFAULT_SHAPE = ScaleShape()


def _mix(*parts: int) -> int:
    """A splitmix64-style hash of the integers: fast, deterministic, platform independent."""
    h = 0x9E3779B97F4A7C15
    for p in parts:
        h = (h + (p & _MASK) + 0x9E3779B97F4A7C15) & _MASK
        h = ((h ^ (h >> 30)) * 0xBF58476D1CE4E5B9) & _MASK
        h = ((h ^ (h >> 27)) * 0x94D049BB133111EB) & _MASK
        h ^= h >> 31
    return h


def _period(shape: ScaleShape) -> int:
    return max(2, round(1 / shape.view_share))


def schema_name(k: int) -> str:
    return f"{ENTITIES[k % len(ENTITIES)]}_{k:04d}"


def table_name(i: int) -> str:
    return f"{ENTITIES[i % len(ENTITIES)]}_{i:07d}"


def table_index(name: str) -> int:
    return int(name.rsplit("_", 1)[1])


def table_ref(i: int, shape: ScaleShape = DEFAULT_SHAPE) -> ObjectRef:
    return ObjectRef(None, schema_name(i // shape.tables_per_schema), table_name(i))


def is_view(i: int, shape: ScaleShape = DEFAULT_SHAPE) -> bool:
    p = _period(shape)
    return i % p == p - 1


def view_level(i: int, shape: ScaleShape = DEFAULT_SHAPE) -> int:
    """1 for a view over plain tables; ``L`` for a view over a level ``L - 1`` view."""
    return 1 + (i // _period(shape)) % shape.max_lineage_depth


def upstream_of(i: int, shape: ScaleShape = DEFAULT_SHAPE) -> tuple[int, ...]:
    """The tables a view reads (empty for a table); always lower indices, so acyclic."""
    if not is_view(i, shape):
        return ()
    if view_level(i, shape) == 1 or i < _period(shape):
        return (i - 2, i - 1) if i >= 2 else (max(i - 1, 0),)
    return (i - _period(shape), i - 1)


def parent_of(i: int, k: int, shape: ScaleShape = DEFAULT_SHAPE) -> int | None:
    """The ``k``-th foreign-key target of non-view table ``i`` (a lower non-view table)."""
    if i == 0 or is_view(i, shape):
        return None
    p = _mix(shape.seed, i, k) % i
    while p > 0 and is_view(p, shape):
        p -= 1
    return None if is_view(p, shape) else p


def column_names(shape: ScaleShape) -> list[str]:
    names = ["id", *(f"parent{k}_id" for k in range(shape.fks_per_table))]
    names += [
        f"{ATTRIBUTES[j % len(ATTRIBUTES)]}_{j}" for j in range(len(names), shape.columns_per_table)
    ]
    return names[: shape.columns_per_table]


def definition(i: int, shape: ScaleShape = DEFAULT_SHAPE) -> str:
    ups = upstream_of(i, shape)
    ref = table_ref(i, shape)
    names = column_names(shape)
    extra = names[min(3, len(names) - 1)]
    refs = [table_ref(u, shape) for u in ups]
    sources = f"{refs[0].schema}.{refs[0].table} AS a"
    if len(refs) > 1:
        sources += f" JOIN {refs[1].schema}.{refs[1].table} AS b ON a.id = b.id"
    cols = f"a.id AS id, a.{extra} AS {extra}"
    return f"CREATE VIEW {ref.schema}.{ref.table} AS SELECT {cols} FROM {sources}"


def changed_set(shape: ScaleShape, fraction: float = 0.01, seed: int = 1) -> frozenset[int]:
    n = max(1, round(shape.tables * fraction))
    return frozenset(random.Random(seed).sample(range(shape.tables), min(n, shape.tables)))


def synthetic_join_facts(shape: ScaleShape) -> Iterator[Fact]:
    """MEASURED ``JOIN`` facts for the declared FK pairs. SYNTHETIC: the rates are a hash of
    the pair, not measured on data; ``source`` says so."""
    for i in range(shape.tables):
        for k in range(shape.fks_per_table):
            p = parent_of(i, k, shape)
            if p is None:
                continue
            child, parent = table_ref(i, shape), table_ref(p, shape)
            join = JoinRef(
                (ObjectRef(child.catalog, child.schema, child.table, f"parent{k}_id"),),
                (ObjectRef(parent.catalog, parent.schema, parent.table, "id"),),
            )
            rate = 0.90 + (_mix(shape.seed, i, k, 7) % 1000) / 10_000
            left = 1000 + _mix(shape.seed, i, k, 8) % 9000
            matched = int(left * rate)
            value: dict[str, Any] = {key: None for key in JOIN_VALUE_KEYS}
            value.update(
                match_rate=matched / left,
                coverage=min(1.0, matched / (left + 500)),
                row_match_rate=matched / left,
                distinct_left=left,
                distinct_right=left + 500,
                matched=matched,
                left_non_null=left,
                fan_in=1.0,
                cardinality="N:1",
                verdict="OK",
            )
            yield Fact.make(
                FactKind.JOIN,
                join,
                value,
                Provenance.MEASURED,
                Status.OBSERVED,
                method="exact",
                source="benchmark:synthetic",
                observed_at=BASE,
            )


class SyntheticReader:
    """A lazy ``CatalogReader`` + ``ViewDefinitionReader``: pages are built on request."""

    dialect = "duckdb"

    def __init__(
        self,
        shape: ScaleShape,
        *,
        changed: frozenset[int] = frozenset(),
        changed_at: datetime | None = None,
    ) -> None:
        self.shape = shape
        self.changed = changed
        self.changed_at = changed_at
        self._names = column_names(shape)

    def _altered(self, i: int) -> datetime:
        return self.changed_at if self.changed_at and i in self.changed else BASE

    def list_catalogs(self) -> Iterator[str]:
        return iter(())

    def list_schemas(self, catalog: str | None, *, page_token: str | None) -> Page[Schema]:
        s = self.shape
        n = (s.tables + s.tables_per_schema - 1) // s.tables_per_schema
        return Page([Schema(None, schema_name(k)) for k in range(n)], None)

    def list_tables(
        self, schema: Schema, *, page_token: str | None, changed_since: datetime | None
    ) -> Page[Table]:
        s = self.shape
        k = int(schema.name.rsplit("_", 1)[1])
        lo, hi = k * s.tables_per_schema, min((k + 1) * s.tables_per_schema, s.tables)
        i = int(page_token) if page_token else lo
        items: list[Table] = []
        while i < hi and len(items) < PAGE_SIZE:
            altered = self._altered(i)
            if changed_since is None or altered > changed_since:
                comment = (
                    f"{ENTITIES[i % len(ENTITIES)]} record {i}"
                    if _mix(s.seed, i, 3) % 1000 < s.comment_share * 1000
                    else None
                )
                items.append(
                    Table(
                        table_ref(i, s),
                        "view" if is_view(i, s) else "table",
                        comment=comment,
                        row_count=_mix(s.seed, i, 4) % 1_000_000,
                        last_altered=altered,
                    )
                )
            i += 1
        # skip trailing unchanged tables so the last page does not point at nothing
        return Page(items, str(i) if i < hi else None)

    def columns(self, tables: Sequence[ObjectRef]) -> Mapping[ObjectRef, list[Column]]:
        out: dict[ObjectRef, list[Column]] = {}
        for t in tables:
            cols: list[Column] = []
            for n, name in enumerate(self._names, start=1):
                fam = "integer" if n <= 1 + self.shape.fks_per_table else _FAMILIES[n % 6]
                cols.append(
                    Column(
                        ObjectRef(t.catalog, t.schema, t.table, name),
                        n,
                        _NATIVE[fam],
                        fam,
                        n > 1,
                    )
                )
            out[t] = cols
        return out

    def constraints(self, tables: Sequence[ObjectRef]) -> Mapping[ObjectRef, list[Constraint]]:
        out: dict[ObjectRef, list[Constraint]] = {}
        s = self.shape
        for t in tables:
            i = table_index(t.table)
            if is_view(i, s):
                out[t] = []
                continue
            cs = [Constraint("primary_key", t, ("id",), enforced=True)]
            for k in range(s.fks_per_table):
                p = parent_of(i, k, s)
                if p is not None:
                    cs.append(
                        Constraint(
                            "foreign_key",
                            t,
                            (f"parent{k}_id",),
                            ref_table=table_ref(p, s),
                            ref_columns=("id",),
                            enforced=False,
                        )
                    )
            out[t] = cs
        return out

    def view_definitions(self, tables: Sequence[ObjectRef]) -> Mapping[ObjectRef, str]:
        s = self.shape
        return {t: definition(i, s) for t in tables if is_view(i := table_index(t.table), s)}

    def capabilities(self) -> ReaderCapabilities:
        return ReaderCapabilities(
            comments=True,
            stats=True,
            last_altered=True,
            constraints=True,
            incremental=True,
            view_definitions=True,
        )
