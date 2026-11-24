import pytest

from schemaguru.model.refs import ObjectRef, WarehouseId


def test_ref_exact_identity() -> None:
    assert ObjectRef(None, "s", "Orders") != ObjectRef(None, "s", "orders")


@pytest.mark.parametrize(
    "ref",
    [
        ObjectRef(None, None, "orders"),
        ObjectRef(None, "shop", "orders"),
        ObjectRef("db", "shop", "orders"),
        ObjectRef("db", "shop", "orders", "id"),
        ObjectRef(None, "shop", "Order Items"),
        ObjectRef(None, "shop", "Order Items", "unit price"),
        ObjectRef(None, "s", 'a"b'),
        ObjectRef(None, "s", "a.b", "c.d"),
        ObjectRef("c.x", "s.y", "t$1", "1col"),
    ],
)
def test_canonical_roundtrip(ref: ObjectRef) -> None:
    assert ObjectRef.parse(ref.canonical(), has_column=ref.column is not None) == ref


def test_canonical_quoting() -> None:
    assert ObjectRef(None, "s", "Order Items").canonical() == 's."Order Items"'
    assert ObjectRef(None, "s", 'a"b').canonical() == 's."a""b"'
    assert ObjectRef(None, "s", "a.b").canonical() == 's."a.b"'
    assert ObjectRef(None, "s", "t", "c").table_ref() == ObjectRef(None, "s", "t")


def test_parse_four_parts_is_column_and_errors() -> None:
    assert ObjectRef.parse("d.s.t.c") == ObjectRef("d", "s", "t", "c")
    assert ObjectRef.parse("s.t") == ObjectRef(None, "s", "t")
    for bad in ["", "a.b.c.d.e", 'a."b', "a..b", ".a"]:
        with pytest.raises(ValueError):
            ObjectRef.parse(bad)


def test_invalid_refs() -> None:
    with pytest.raises(ValueError):
        ObjectRef("c", None, "t")
    with pytest.raises(ValueError):
        ObjectRef(None, "s", "")


def test_warehouse_id() -> None:
    assert WarehouseId("prod", "duckdb").name == "prod"
