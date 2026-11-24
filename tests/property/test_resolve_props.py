import random
from datetime import datetime, timedelta, timezone

from hypothesis import given
from hypothesis import strategies as st

from schemaguru.knowledge.resolve import resolve
from schemaguru.model.facts import Fact, Provenance, Status
from schemaguru.model.kinds import FactKind
from schemaguru.model.refs import ObjectRef

NOW = datetime(2026, 11, 24, tzinfo=timezone.utc)
COLS = [ObjectRef(None, "shop", "orders", c) for c in ("status", "total")]
TBL = ObjectRef(None, "shop", "orders")


def _fact(i: int, prov: Provenance, days: int, v: str) -> Fact:
    kind, subj = [
        (FactKind.DESCRIPTION, TBL),
        (FactKind.VALUES, COLS[0]),
        (FactKind.PII, COLS[1]),
        (FactKind.ROW_COUNT, TBL),
    ][i % 4]
    status = Status.OBSERVED if prov is Provenance.MEASURED else Status.UNVERIFIED
    value = (
        {"values": [v], "observed": [{"value": v + "x", "count": 2}]}
        if kind is FactKind.VALUES
        else {"category": v, "text": v, "rows": len(v)}
    )
    return Fact.make(
        kind,
        subj,
        value,
        prov,
        status,
        method="exact" if prov is Provenance.MEASURED else None,
        observed_at=NOW - timedelta(days=days),
    )


facts = st.lists(
    st.builds(
        _fact,
        st.integers(0, 3),
        st.sampled_from(list(Provenance)),
        st.integers(0, 40),
        st.sampled_from(["a", "b", "c"]),
    ),
    max_size=12,
)


@given(facts, st.integers(0, 1000))
def test_resolve_order_independent(fs: list[Fact], seed: int) -> None:
    shuffled = list(fs)
    random.Random(seed).shuffle(shuffled)
    r1 = resolve(fs, now=NOW, ttl_days=14)
    r2 = resolve(shuffled, now=NOW, ttl_days=14)
    assert list(r1) == list(r2)
    assert r1 == r2
