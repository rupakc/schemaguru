from __future__ import annotations

import tempfile
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from schemaguru.model.refs import ObjectRef
from schemaguru.model.structure import Column, Table
from schemaguru.overlays import load_overlays
from schemaguru.store.sqlite import SQLiteStore

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
