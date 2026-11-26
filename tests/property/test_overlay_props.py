from __future__ import annotations

import tempfile
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from schemaguru.model.refs import ObjectRef
from schemaguru.model.structure import Column, Table
from schemaguru.overlays import load_overlays
from schemaguru.store.sqlite import SQLiteStore

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

_KEYS = [
    "version",
    "tables",
    "shop.orders",
    "description",
    "columns",
    "status",
    "values",
    "joins",
    "to",
    "from",
    "chains",
    "steps",
    "waypoints",
    "pair",
    "require",
    "waivers",
    "expires",
    "fact",
    "glossary",
    "never_query",
    "table",
    "reason",
    "because",
    "name",
]
_SCALARS = st.one_of(
    st.sampled_from(
        [
            "1",
            "2",
            "null",
            "~",
            "true",
            "2027-01-01",
            "shop.orders",
            "a -> b",
            "''",
            '"("',
            "[",
            "]",
            "{",
            "&x",
            "*x",
            "!!str 1",
            "|",
            ":",
        ]
    ),
    st.text(max_size=12),
)


def _yamlish() -> st.SearchStrategy[str]:
    line = st.tuples(st.integers(0, 4), st.sampled_from(_KEYS), _SCALARS, st.booleans()).map(
        lambda t: f"{'  ' * t[0]}{'- ' if t[3] else ''}{t[1]}: {t[2]}"
    )
    return st.lists(line, max_size=12).map(lambda ls: "version: 1\n" + "\n".join(ls))


def _store(tmp: Path) -> SQLiteStore:
    s = SQLiteStore(tmp / "p.db")
    ref = ObjectRef(None, "shop", "orders")
    s.upsert_tables([Table(ref, "table")])
    s.upsert_columns(
        ref, [Column(ObjectRef(None, "shop", "orders", "status"), 1, "S", "S", True, None, None)]
    )
    return s


@settings(
    max_examples=150, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(
    st.one_of(
        st.text(max_size=200),
        st.binary(max_size=100).map(lambda b: b.decode("latin-1")),
        _yamlish(),
    )
)
def test_loader_never_raises(text: str) -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        (tmp / "o.yaml").write_bytes(text.encode("utf-8", "replace"))
        s = _store(tmp)
        try:
            *_, bag = load_overlays([tmp / "o.yaml"], s)
            assert isinstance(list(bag), list)
        finally:
            s.close()


_DBT_KEYS = [
    "nodes",
    "sources",
    "resource_type",
    "name",
    "schema",
    "database",
    "alias",
    "description",
    "columns",
    "attached_node",
    "depends_on",
    "test_metadata",
    "kwargs",
    "column_name",
    "to",
    "field",
    "values",
    "model",
    "test",
    "orders",
    "customers",
    "compiled_code",
    "metadata",
    "adapter_type",
    "config",
    "materialized",
    "identifier",
    "ephemeral",
    "postgres",
]
_JSONISH = st.recursive(
    st.one_of(st.none(), st.booleans(), st.integers(), st.text(max_size=8)),
    lambda c: st.one_of(
        st.lists(c, max_size=3), st.dictionaries(st.sampled_from(_DBT_KEYS), c, max_size=4)
    ),
    max_leaves=20,
)


@settings(max_examples=150, deadline=None)
@given(_JSONISH)
def test_import_dbt_never_raises(manifest: object) -> None:
    import json

    from schemaguru.diagnostics import DiagnosticBag
    from schemaguru.overlays import import_dbt

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        (tmp / "m.json").write_text(json.dumps(manifest), encoding="utf-8")
        s = _store(tmp)
        try:
            import_dbt(tmp / "m.json", s, diagnostics=DiagnosticBag())
        finally:
            s.close()


_LINEAGE_FIELDS = ["compiled_code", "depends_on", "config", "alias", "identifier", "schema", "name"]


@settings(max_examples=120, deadline=None)
@given(
    st.sampled_from(_LINEAGE_FIELDS),
    st.sampled_from(["model.shop.orders", "model.shop.revenue", "model.shop.eph"]),
    _JSONISH,
    _JSONISH,
)
def test_import_dbt_lineage_never_raises(field: str, uid: str, junk: object, meta: object) -> None:
    """A real lineage manifest with one field of one node (and the metadata) replaced by junk:
    ``apply_dbt`` reports, never raises, and never keeps junk lineage."""
    import json

    from schemaguru.diagnostics import DiagnosticBag
    from schemaguru.overlays import apply_dbt

    manifest = json.loads((FIXTURES / "dbt" / "manifest_lineage.json").read_text("utf-8"))
    manifest["nodes"][uid][field] = junk
    manifest["metadata"] = meta
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        (tmp / "m.json").write_text(json.dumps(manifest), encoding="utf-8")
        s = _store(tmp)
        try:
            bag = DiagnosticBag()
            facts = apply_dbt(tmp / "m.json", s, diagnostics=bag)
            assert all(f.status.value == "UNVERIFIED" for f in facts if "LINEAGE" in f.kind.name)
        finally:
            s.close()
