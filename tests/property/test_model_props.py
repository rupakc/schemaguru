from datetime import datetime, timezone

from hypothesis import given
from hypothesis import strategies as st

from schemaguru.model.facts import Fact, Provenance, Status
from schemaguru.model.kinds import FactKind
from schemaguru.model.refs import ObjectRef

names = st.text(min_size=1, max_size=8)
refs = st.builds(
    ObjectRef,
    st.none(),
    st.one_of(st.none(), names),
    names,
    st.one_of(st.none(), names),
).filter(lambda r: True)
json_vals = st.recursive(
    st.one_of(st.none(), st.booleans(), st.integers(), st.floats(allow_nan=False), st.text()),
    lambda c: st.one_of(
        st.lists(c, max_size=3), st.dictionaries(st.text(max_size=4), c, max_size=3)
    ),
    max_leaves=8,
)


@given(refs)
def test_ref_roundtrip(ref: ObjectRef) -> None:
    assert ObjectRef.parse(ref.canonical(), has_column=ref.column is not None) == ref


@given(
    st.dictionaries(st.text(max_size=5), json_vals, max_size=4),
    st.sampled_from(list(Status)),
    st.sampled_from(list(Provenance)),
    st.one_of(st.none(), st.datetimes(timezones=st.just(timezone.utc))),
)
def test_fact_dict_roundtrip(
    value: dict[str, object], status: Status, prov: Provenance, at: datetime | None
) -> None:
    method = "sampled:system:0.01:1" if status is Status.ESTIMATED else "exact"
    if status is Status.VERIFIED and prov in (Provenance.HUMAN, Provenance.DECLARED):
        return
    f = Fact.make(
        FactKind.DESCRIPTION,
        ObjectRef(None, "s", "t"),
        value,
        prov,
        status,
        method=method,
        observed_at=at,
    )
    again = Fact.from_dict(f.to_dict())
    assert again == f and again.id == f.id


def test_stable_hash_normalises() -> None:
    from datetime import date
    from decimal import Decimal

    from schemaguru._util.hashing import canonical_json, stable_hash

    assert stable_hash(Decimal("1.0")) == stable_hash(Decimal("1.00")) == stable_hash(Decimal("1"))
    assert stable_hash(Decimal("100")) == stable_hash(Decimal("1E+2"))
    assert stable_hash(Decimal("-0.00")) == stable_hash(Decimal("0"))
    assert stable_hash(Decimal("1.5")) != stable_hash(Decimal("1.50001"))
    assert canonical_json(Decimal("12.50")) == '"12.5"'
    assert canonical_json(date(2026, 1, 2)) == '"2026-01-02"'
    assert stable_hash(date(2026, 1, 2)) == stable_hash("2026-01-02")
    # mixed-type keys sort as their JSON text instead of raising TypeError
    assert stable_hash({1: "a", "b": 2}) == stable_hash({"b": 2, 1: "a"})
    assert canonical_json({None: 1, "a": 2, 3: 4}) == '{"3":4,"a":2,"null":1}'
    # 0.1.0 ids are unchanged: plain JSON hashes exactly as before
    assert stable_hash({"b": [1, 2.5, None], "a": "x"}) == (
        "sha256:" + __import__("hashlib").sha256(b'{"a":"x","b":[1,2.5,null]}').hexdigest()
    )
    assert stable_hash({10: "x", 9: "y"}) == (  # all-int keys keep their numeric order
        "sha256:" + __import__("hashlib").sha256(b'{"9":"y","10":"x"}').hexdigest()
    )


decimals = st.decimals(allow_nan=False, allow_infinity=False, places=None)
dates = st.dates()


@given(st.dictionaries(st.text(max_size=4), st.one_of(decimals, dates), min_size=1, max_size=4))
def test_decimal_date_fact_roundtrips_id(value: dict[str, object]) -> None:
    import json

    from schemaguru._util.hashing import canonical_json

    for kind in (FactKind.DESCRIPTION, FactKind.ROW_COUNT):
        f = Fact.make(kind, ObjectRef(None, "s", "t"), value, Provenance.HUMAN, Status.UNVERIFIED)
        assert Fact.from_dict(json.loads(canonical_json(f.to_dict()))).id == f.id


UTC_ONLY = st.just(timezone.utc)


def _nr_at(rate: float, at: datetime | None, src: str) -> Fact:
    return Fact.make(
        FactKind.NULL_RATE, ObjectRef(None, "s", "t", "c"), {"rate": rate},
        Provenance.MEASURED, Status.OBSERVED, method="exact", observed_at=at, source=src,
    )  # fmt: skip


@given(
    st.lists(
        st.tuples(
            st.floats(0, 1),
            st.one_of(st.none(), st.datetimes(datetime(2000, 1, 1), timezones=UTC_ONLY)),
            st.sampled_from(["a", "b"]),
        ),
        min_size=1,
        max_size=5,
    ),
    st.randoms(),
)
def test_diff_resolve_same_tiebreak(
    rows: list[tuple[float, datetime | None, str]], rnd: object
) -> None:
    from schemaguru.knowledge.diff import diff
    from schemaguru.knowledge.resolve import resolve

    facts = [_nr_at(*r) for r in rows]  # one id: every re-measure of one column
    shuffled = list(facts)
    rnd.shuffle(shuffled)  # type: ignore[attr-defined]
    far = datetime(2100, 1, 1, tzinfo=timezone.utc)
    (eff,) = resolve(shuffled, now=far, ttl_days=100_000).values()
    (change,) = diff([], shuffled)
    assert change.after is not None and eff.primary is not None
    assert change.after.to_dict() == eff.primary.to_dict()
