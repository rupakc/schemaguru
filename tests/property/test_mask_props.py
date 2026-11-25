from __future__ import annotations

import base64

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


# --- recall guard for the 0.2.0 precision trades (ledger A1.11 paths, A1.14 SQL types) --------
# Every value here was masked by 0.1.0 with this category (checked at the parent commit); a
# precision fix may add masks elsewhere but must keep every one of these.
_GH = "gh" + "p_" + "Zx9Qm2Lp8Rt4Vb7Nc1Kd6Hs3Jf5Wy0Ue4Ra7Bc"
RECALL_CORPUS: tuple[tuple[str, str], ...] = (
    ("jane@example.com", "email"),
    ("a@b.com, c@d.org", "email"),
    ("+4915123456789", "phone"),
    ("+44 20 7946 0958", "phone"),
    ("00491701234567", "phone"),
    ("0012345678901", "phone"),
    ("000-123-4567", "phone"),
    ("DE89370400440532013000", "iban"),
    ("4111 1111 1111 1111", "card"),
    ("10.1.2.3", "ip"),
    ("2.10.3.1", "ip"),
    ("::1", "ip"),
    ("123e4567-e89b-12d3-a456-426614174000", "uuid"),
    ("d41d8cd98f00b204e9800998ecf8427e", "hex"),
    ("Maria Garcia", "name"),
    ("Austin Store", "name"),
    ("221B Baker Street", "address"),
    ("Hauptstraße 12", "address"),
    ("Zx9Qm2Lp8Rt4Vb7Nc1Kd6Hs3Jf5aa", "secret"),
    ("wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "secret"),
    ("/srv/keys/Zx9Qm2Lp8Rt4Vb7Nc1Kd6Hs3Jf5/id", "secret"),
    ("/" + _GH, "secret"),
    ("api_key=Zx9Qm2Lp8Rt4Vb7Nc1Kd6Hs3Jf5aa", "secret"),
    ("password=hunter2", "secret"),
    ("password: hunter2hunter2", "secret"),
    ("password varchar255abc", "secret"),
    ("retry with password: hunter2hunter2", "secret"),
    # separator-segmented keys under a path (fix round 1: all masked by 0.1.0)
    *(
        (prefix + key, "secret")
        for prefix in ("/", "~/", "./")
        for key in (
            "licenses/AB3X-QZ9K-MN2P-WR7T-YU5V",
            "keys/aB3x-Qz9K-mN2p-Wr7T-yU5v",
            "reset/k2m8z-p4w7r-j6n1t-b5vq9",
            "api/v1/keys/X7kP-2mQz-9LrT-4vBn-8cWs",
            "tmp/Zx9Q.m2Lp.8Rt4.Vb7N.c1Kd.6Hs3",
            "s/3f9K_x2Lq_8mZp_4vRt_7bNc_1yWs",
        )
    ),
)

# base64 tokens (which may hold '/') and passwords after a credential key, as 0.1.0 masked them
B64_TOKENS = (
    st.binary(min_size=18, max_size=40)
    .map(lambda b: base64.b64encode(b).decode().rstrip("="))
    .filter(lambda s: len(s) >= 24 and _v010_secret(s))
)
# urlsafe base64 ('-' and '_' split it into pieces) and keys written in groups ("AB3X-QZ9K-...")
URLSAFE_TOKENS = (
    st.binary(min_size=18, max_size=40)
    .map(lambda b: base64.urlsafe_b64encode(b).decode().rstrip("="))
    .filter(lambda s: _v010_secret(s))
)
SEGMENTED_KEYS = st.builds(
    lambda groups, sep: sep.join(groups),
    st.lists(st.from_regex(r"[A-Za-z0-9]{4,6}", fullmatch=True), min_size=4, max_size=8),
    st.sampled_from(["-", "_", "."]),
).filter(lambda s: _v010_secret(s))
PATH_PREFIXES = st.sampled_from(["/", "~/", "./", "/keys/", "/tmp/a/", "s3://bucket/"])
PATH_TOKENS = st.builds(
    lambda prefix, token: prefix + token,
    PATH_PREFIXES,
    st.one_of(B64_TOKENS, URLSAFE_TOKENS, SEGMENTED_KEYS),
)
# hex/UUID-keyed paths: a key segment beside ordinary-looking segments (fix round 2)
_KEY_SEGMENTS = st.one_of(
    st.from_regex(r"[0-9a-f]{20,40}", fullmatch=True),
    st.uuids().map(str),
)
_OTHER_SEGMENTS = st.one_of(
    st.from_regex(r"[a-z]{3,12}", fullmatch=True),
    st.from_regex(r"[A-Z][a-z]{2,10}", fullmatch=True),
    st.from_regex(r"[a-z]{4,6}(?:-[a-z]{4,6}){1,4}", fullmatch=True),
    st.from_regex(r"[a-z]{3,10}\.[a-z]{2,4}", fullmatch=True),
)
HEX_KEYED_PATHS = st.builds(
    lambda prefix, segments: prefix + "/".join(segments),
    st.sampled_from(["/", "~/", "./"]),
    st.lists(st.one_of(_KEY_SEGMENTS, _OTHER_SEGMENTS), min_size=2, max_size=5).filter(
        lambda segs: any(
            len(x) >= 20 and x.replace("-", "").isalnum() and not x.isalpha() for x in segs
        )
    ),
).filter(lambda s: _v010_secret(s))
PASSWORDS = st.from_regex(r"[A-Za-z]{2,10}[0-9]{1,4}[A-Za-z0-9!#%]{0,8}", fullmatch=True)


def _v010_secret(v: str) -> bool:
    """0.1.0's whole-value entropy-secret rule, verbatim (the reference the guard keeps)."""
    import re

    from schemaguru.pii.detectors import UUID_RE, hex_ok, shannon

    if len(v) < 24 or not re.fullmatch(r"[A-Za-z0-9_\-+/=.~]+", v):
        return False
    if re.match(r"[A-Za-z][A-Za-z0-9+.\-]*://", v) or UUID_RE.fullmatch(v) or hex_ok(v):
        return False
    if not (any(c.isdigit() for c in v) and any(c.isalpha() for c in v)):
        return False
    return shannon(v) >= 4.0


@settings(max_examples=600, deadline=None)
@given(
    st.one_of(
        st.sampled_from(RECALL_CORPUS),
        PATH_TOKENS.map(lambda s: (s, "secret")),
        HEX_KEYED_PATHS.map(lambda s: (s, "secret")),
        URLSAFE_TOKENS.map(lambda s: (s, "secret")),
        SEGMENTED_KEYS.map(lambda s: (s, "secret")),
        B64_TOKENS.map(lambda s: (s, "secret")),
        B64_TOKENS.map(lambda s: "/" + s[1:]).filter(_v010_secret).map(lambda s: (s, "secret")),
        PASSWORDS.filter(lambda p: len(p) >= 8).map(lambda p: (f"password: {p}", "secret")),
        PASSWORDS.map(lambda p: (f"password={p}", "secret")),
    )
)
def test_recall_not_reduced(case: tuple[str, str]) -> None:
    value, category = case
    out, cat = mask_value(value)
    assert cat == category and out != value, (value, out, cat)
