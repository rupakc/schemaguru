from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from schemaguru.model.evidence import Evidence
from schemaguru.model.facts import Fact, Provenance, Status
from schemaguru.model.kinds import LINEAGE_KINDS, FactKind
from schemaguru.model.lineage import (
    DBT_SOURCE,
    TRANSFORMS,
    VIEW_SOURCE,
    LineageEdge,
    LineageLink,
    column_lineage_value,
    edges_of,
    resolve_lineage,
    source_label,
    table_lineage_value,
)
from schemaguru.model.refs import ObjectRef

T = ObjectRef(None, "analytics", "revenue_daily")
C = ObjectRef(None, "analytics", "revenue_daily", "net_revenue")
_P, _S = Provenance, Status


def _tl(
    upstream: list[str] | None,
    prov: Provenance = _P.DECLARED,
    *,
    source: str | None = "dbt:manifest:model.shop.revenue_daily",
    unresolved: tuple[str, ...] = (),
    ignore: bool = False,
) -> Fact:
    value = table_lineage_value(upstream, unresolved, ignore=ignore)
    return Fact.make(FactKind.LINEAGE, T, value, prov, _S.UNVERIFIED, source=source)


def _cl(
    inputs: list[str] | None,
    transform: str | None = "aggregate",
    prov: Provenance = _P.CATALOG,
    *,
    source: str | None = VIEW_SOURCE,
    ignore: bool = False,
) -> Fact:
    value = column_lineage_value(inputs, transform, ignore=ignore)
    return Fact.make(FactKind.COLUMN_LINEAGE, C, value, prov, _S.UNVERIFIED, source=source)


def test_kinds_registered() -> None:
    assert frozenset({"LINEAGE", "COLUMN_LINEAGE"}) == LINEAGE_KINDS
    assert FactKind.LINEAGE.subject == "table"
    assert FactKind.COLUMN_LINEAGE.subject == "column"
    assert FactKind.LINEAGE.identity_fields == ("upstream", "ignore")
    assert FactKind.COLUMN_LINEAGE.identity_fields == ("inputs", "ignore")
    assert TRANSFORMS == ("copy", "cast", "expression", "aggregate", "window", "unknown")
    assert (DBT_SOURCE, VIEW_SOURCE) == ("dbt:manifest", "view_definition")


def test_lineage_never_verified() -> None:
    value = table_lineage_value(["shop.orders"])
    for status in (_S.VERIFIED, _S.OBSERVED):
        with pytest.raises(ValueError, match="never measured or verified"):
            Fact.make(FactKind.LINEAGE, T, value, _P.HUMAN, status, evidence=Evidence())
    with pytest.raises(ValueError, match="never measured or verified"):
        Fact.make(
            FactKind.LINEAGE, T, value, _P.MEASURED, _S.UNVERIFIED,
            method="exact", evidence=Evidence(),
        )  # fmt: skip
    with pytest.raises(ValueError, match="never measured or verified"):
        Fact.make(FactKind.COLUMN_LINEAGE, C, column_lineage_value(["a.b.c"], "copy"),
                  _P.DRAFTED, _S.UNVERIFIED)  # fmt: skip
    for prov in (_P.DECLARED, _P.CATALOG, _P.HUMAN):  # the control: these are allowed
        assert Fact.make(FactKind.LINEAGE, T, value, prov, _S.UNVERIFIED).status is _S.UNVERIFIED


def test_value_builders_sort_and_validate() -> None:
    assert table_lineage_value(["b", "a", "a"])["upstream"] == ["a", "b"]
    assert table_lineage_value(["a"], ["z", "y", "z"]) == {
        "upstream": ["a"], "unresolved": ["y", "z"], "ignore": False,
    }  # fmt: skip
    assert table_lineage_value(None, ignore=True)["upstream"] is None
    with pytest.raises(ValueError):
        table_lineage_value(None)
    with pytest.raises(ValueError):
        column_lineage_value(["x"], "sum")
    with pytest.raises(ValueError):
        column_lineage_value(["x"], None)
    with pytest.raises(ValueError):
        column_lineage_value(None, "copy")
    assert column_lineage_value(["y", "x"], "copy") == {
        "inputs": ["x", "y"], "transform": "copy", "unresolved": [], "ignore": False,
    }  # fmt: skip
    assert column_lineage_value(None, None, ignore=True)["transform"] is None


def test_id_ignores_source() -> None:
    a = _tl(["shop.orders"], source="dbt:manifest:model.a")
    b = _tl(["shop.orders"], source="dbt:manifest:model.b")
    assert a.id == b.id
    assert _tl(["shop.orders"]).id != _tl(["shop.refunds"]).id  # the list is identity


def test_ignore_in_identity() -> None:
    replace_ = _tl(["shop.orders"], _P.HUMAN, source="overlay:l.yml:3")
    ignore = _tl(["shop.orders"], _P.HUMAN, source="overlay:l.yml:3", ignore=True)
    assert replace_.id != ignore.id


def test_source_label() -> None:
    assert source_label("dbt:manifest:model.shop.x") == "dbt:manifest"
    assert source_label("dbt:manifest") == "dbt:manifest"
    assert source_label("view_definition") == "view_definition"
    assert source_label("overlay:lineage.yml:4") == "overlay:lineage.yml:4"
    assert source_label("something") == "something"
    assert source_label(None) == "unknown"
    assert source_label("") == "unknown"


def test_human_replaces_parsed_per_level() -> None:
    dbt = _tl(["shop.orders"])
    human = _tl(["shop.orders", "shop.refunds"], _P.HUMAN, source="overlay:l.yml:2")
    col = _cl(["shop.orders.amount"])
    res = resolve_lineage([dbt, human, col])
    assert [(lk.level, lk.inputs) for lk in res.links] == [  # sorted by subject first
        ("table", ("shop.orders", "shop.refunds")),
        ("column", ("shop.orders.amount",)),
    ]
    table_link = next(lk for lk in res.links if lk.level == "table")
    assert table_link.sources == ("overlay:l.yml:2",)
    assert table_link.fact_ids == (human.id,)
    assert res.hidden == (dbt,)
    assert res.unmatched_ignores == ()


def test_each_human_list_is_its_own_link() -> None:
    h1 = _tl(["shop.orders"], _P.HUMAN, source="overlay:l.yml:2")
    h2 = _tl(["shop.refunds"], _P.HUMAN, source="overlay:l.yml:5")
    res = resolve_lineage([h1, h2, _tl(["shop.carts"])])
    assert [lk.inputs for lk in res.links] == [("shop.orders",), ("shop.refunds",)]


def test_ignore_without_list_hides_all() -> None:
    dbt = _tl(["shop.orders"])
    view = _tl(["shop.carts"], _P.CATALOG, source=VIEW_SOURCE)
    ign = _tl(None, _P.HUMAN, source="overlay:l.yml:8", ignore=True)
    col = _cl(["shop.orders.amount"])
    res = resolve_lineage([dbt, view, ign, col])
    assert [lk.level for lk in res.links] == ["column"]  # the column level is untouched
    assert {f.id for f in res.hidden} == {dbt.id, view.id}
    assert res.unmatched_ignores == ()


def test_ignore_with_list_hides_exact_only() -> None:
    ab = _tl(["b", "a"])
    a = _tl(["a"], _P.CATALOG, source=VIEW_SOURCE)
    ign = _tl(["a", "b"], _P.HUMAN, source="overlay:l.yml:8", ignore=True)
    res = resolve_lineage([ab, a, ign])
    assert [lk.inputs for lk in res.links] == [("a",)]
    assert res.hidden == (ab,)
    assert res.unmatched_ignores == ()


def test_unmatched_ignore_reported() -> None:
    dbt = _tl(["shop.orders"])
    ign = _tl(["shop.refunds"], _P.HUMAN, source="overlay:l.yml:8", ignore=True)
    nothing = _cl(None, None, _P.HUMAN, source="overlay:l.yml:9", ignore=True)
    res = resolve_lineage([dbt, ign, nothing])
    assert [lk.inputs for lk in res.links] == [("shop.orders",)]
    assert res.hidden == ()
    assert {f.id for f in res.unmatched_ignores} == {ign.id, nothing.id}


def test_identical_lists_merge_sources() -> None:
    dbt = _tl(["shop.orders"])
    view = _tl(["shop.orders"], _P.CATALOG, source=VIEW_SOURCE)
    other = _tl(["shop.carts"], _P.CATALOG, source=VIEW_SOURCE)
    res = resolve_lineage([dbt, view, other])
    assert res.links == (
        LineageLink(T.canonical(), "table", ("shop.carts",), None, (), (VIEW_SOURCE,),
                    (other.id,)),
        LineageLink(T.canonical(), "table", ("shop.orders",), None, (),
                    ("dbt:manifest", "view_definition"), tuple(sorted({dbt.id, view.id}))),
    )  # fmt: skip


def test_differing_transform_or_unresolved_not_merged() -> None:
    a = _cl(["x.y.z"], "copy", _P.DECLARED, source="dbt:manifest:model.a")
    b = _cl(["x.y.z"], "cast")
    c = _tl(["shop.orders"], unresolved=("raw.thing",))
    d = _tl(["shop.orders"], _P.CATALOG, source=VIEW_SOURCE)
    res = resolve_lineage([a, b, c, d])
    assert len(res.links) == 4


_POOL: list[Any] = [
    _tl(["shop.orders"]),
    _tl(["shop.orders"], _P.CATALOG, source=VIEW_SOURCE),
    _tl(["shop.orders"], source="dbt:manifest:model.other"),
    _tl(["shop.carts", "shop.orders"], _P.CATALOG, source=VIEW_SOURCE),
    _tl(["shop.carts"], _P.HUMAN, source="overlay:l.yml:1", ignore=True),
    _tl(["shop.carts", "shop.orders"], _P.HUMAN, source="overlay:l.yml:2", ignore=True),
    _cl(["shop.orders.amount"], "aggregate"),
    _cl(["shop.orders.amount"], "copy", _P.DECLARED, source="dbt:manifest:model.a"),
    _cl(["shop.orders.amount", "shop.refunds.amount"], "aggregate", _P.HUMAN,
        source="overlay:l.yml:3"),
    _cl(None, None, _P.HUMAN, source="overlay:l.yml:4", ignore=True),
]  # fmt: skip


@settings(max_examples=60, deadline=None)
@given(st.permutations(_POOL), st.integers(min_value=1, max_value=len(_POOL)))
def test_order_independent(perm: list[Fact], n: int) -> None:
    subset = perm[:n]
    expected = resolve_lineage(sorted(subset, key=lambda f: (f.id, f.source or "")))
    assert resolve_lineage(subset) == expected
    assert resolve_lineage(reversed(subset)) == expected


def test_edges_skip_unresolved() -> None:
    a = _tl(["shop.carts", "shop.orders"], unresolved=("raw.thing",))
    b = _tl(["shop.carts", "shop.orders"], _P.CATALOG, source=VIEW_SOURCE,
            unresolved=("raw.thing",))  # fmt: skip
    col = _cl(["shop.orders.amount"], "copy")
    edges = edges_of(resolve_lineage([a, b, col]))
    ids = sorted({a.id, b.id})
    assert edges == [
        LineageEdge(up, T.canonical(), "table", fid)
        for up in ("shop.carts", "shop.orders") for fid in ids
    ] + [LineageEdge("shop.orders.amount", C.canonical(), "column", col.id)]  # fmt: skip
    assert not any(e.upstream_key == "raw.thing" for e in edges)


def test_non_lineage_facts_ignored() -> None:
    desc = Fact.make(FactKind.DESCRIPTION, T, {"text": "x"}, _P.HUMAN, _S.UNVERIFIED)
    res = resolve_lineage([desc])
    assert res.links == res.hidden == res.unmatched_ignores == ()


def test_round_trip() -> None:
    for f in (_tl(["shop.orders"], unresolved=("raw.x",)), _cl(["a.b.c"], "window")):
        assert Fact.from_dict(f.to_dict()) == f


def test_bare_string_rejected() -> None:
    with pytest.raises(TypeError, match="upstream"):
        table_lineage_value("shop.orders")
    with pytest.raises(TypeError, match="unresolved"):
        table_lineage_value(["shop.orders"], "raw.x")
    with pytest.raises(TypeError, match="inputs"):
        column_lineage_value("shop.orders.amount", "copy")
    with pytest.raises(TypeError, match="unresolved"):
        column_lineage_value(["a.b.c"], "copy", b"raw.x")  # type: ignore[arg-type]
