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
