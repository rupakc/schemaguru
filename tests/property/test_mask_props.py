from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from schemaguru.pii import mask_value, sweep

EMAILS = st.from_regex(r"[a-z0-9._%+\-]{1,20}@[a-z0-9\-]{1,15}\.[a-z]{2,6}", fullmatch=True)

PIIISH = st.one_of(
    st.text(max_size=120),
    EMAILS,
    st.uuids().map(str),
    st.binary(min_size=10, max_size=30).map(lambda b: b.hex()),
    st.integers(min_value=0, max_value=10**19).map(str),
    st.from_regex(r"\+?[0-9 ()\-.]{5,25}", fullmatch=True),
    st.from_regex(r"[A-Z]{2}[0-9]{2}[A-Z0-9 ]{10,30}", fullmatch=True),
    st.from_regex(r"[0-9a-f:.]{3,40}", fullmatch=True),
    st.builds(lambda a, b, c: f"{a} {b} {c}", st.text(max_size=20), EMAILS, st.text(max_size=20)),
)


@settings(max_examples=400, deadline=None)
@given(PIIISH)
def test_mask_idempotent(v: str) -> None:
    once = mask_value(v)[0]
    assert mask_value(once)[0] == once


@settings(max_examples=400, deadline=None)
@given(PIIISH)
def test_sweep_idempotent(v: str) -> None:
    once, _ = sweep(v)
    twice, hits = sweep(once)
    assert twice == once and hits == 0


@settings(max_examples=200, deadline=None)
@given(EMAILS)
def test_email_never_survives(e: str) -> None:
    assert e not in mask_value(e)[0]
    assert e not in sweep(f"error for {e}!")[0]
