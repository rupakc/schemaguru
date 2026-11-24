from __future__ import annotations

import time
from collections.abc import Callable

import pytest

from schemaguru.pii import all_detectors, mask_value, sweep
from schemaguru.pii.sweep import _RULES

N = 50_000
ADVERSARIAL = {
    "a": "a" * N,
    "1": "1" * N,
    "a.b": "a.b" * (N // 3),
    "a@": "a@" * (N // 2),
    "a@b.": "a@b." * (N // 4),
    "1 ": "1 " * (N // 2),
    "01-1 ": "01-111 " * (N // 7),
    "+1 ": "+1 2" * (N // 4),
    ":": ":" * N,
    "1:": "1:" * (N // 2),
    "A1": "A1" * (N // 2),
    "GB82": "GB82" + "A" * N,
    "-": "-" * N,
    "space": " " * N,
    "paren": "(1)" * (N // 3),
    "token=": "token=" * (N // 6),
    "Maria ": "Maria " * (N // 6),
    "A ": "A " * (N // 2),
    "x.": "xxxxxxxxxx." * (N // 11),
    "hex+g": "a" * N + "g",
    "digits-": "1-" * (N // 2),
    "0049": "00" + "1 " * (N // 2),
    "Baker ": "Baker " * (N // 6),
    "AKIA": "AKIA" * (N // 4),
    "ghp_": "gh" + "p_" * (N // 2),
    "ghp_long": "gh" + "p_" + "a" * N,
    "xoxb-": "xox" + "b-" * (N // 2),
    "xoxb-long": "xo" + "xb-" + "a" * N + "!",
    "AIza": "AIza" * (N // 4),
    "AIza-long": "AI" + "za" + "a" * N,
    "sk_": "sk_test_" * (N // 8),
}


SMALL_N = 12_500
# Linear work doubles-to-quadruples when the input grows 4x; quadratic is 16x.
# The ratio, not the wall clock, is what catches backtracking, so it stays
# meaningful under coverage tracing, which slows every line by a similar factor.
MAX_SCALING_RATIO = 8.0
# Generous absolute ceiling: catastrophic backtracking takes seconds, not 2 s.
ABS_CEILING_S = 2.0
# Below this the small run is dominated by timer and scheduler noise.
NOISE_FLOOR_S = 0.005


def _best(fn: Callable[[str], object], arg: str) -> float:
    best = 1e9
    for _ in range(3):
        t = time.perf_counter()
        fn(arg)
        best = min(best, time.perf_counter() - t)
    return best


def _assert_scales_linearly(fn: Callable[[str], object], text: str, label: object) -> None:
    """Assert fn on the 50k input is an absolute-fast, linear-scaling operation."""
    t_small = _best(fn, text[:SMALL_N])
    t_large = _best(fn, text)
    ratio = t_large / max(t_small, NOISE_FLOOR_S)
    assert t_large < ABS_CEILING_S, (label, t_large)
    assert ratio < MAX_SCALING_RATIO, (label, t_small, t_large, round(ratio, 2))


@pytest.mark.parametrize("key", sorted(ADVERSARIAL))
def test_every_sweep_rule_is_fast(key: str) -> None:
    text = ADVERSARIAL[key]
    for category, rx, _ok, _tok in _RULES:
        _assert_scales_linearly(
            lambda s, rx=rx: list(rx.finditer(s)), text, (category, rx.pattern[:40])
        )


@pytest.mark.parametrize("key", sorted(ADVERSARIAL))
def test_every_whole_value_detector_is_fast(key: str) -> None:
    text = ADVERSARIAL[key]
    for d in all_detectors():
        _assert_scales_linearly(d.matches, text, d.name)


@pytest.mark.parametrize("key", sorted(ADVERSARIAL))
def test_mask_and_sweep_are_fast(key: str) -> None:
    text = ADVERSARIAL[key]
    _assert_scales_linearly(mask_value, text, "mask_value")
    _assert_scales_linearly(sweep, text, "sweep")


@pytest.mark.parametrize(
    "value",
    [
        "user@bücher.de",
        "b'maria@x.com'",
        "{'email': 'a@b.co'}",
        "key=a@b.co",
        "mail:jane@example.com;",
        "x@xn--bcher-kva.de",
    ],
)
def test_emails_masked(value: str) -> None:
    out, cat = mask_value(value)
    assert "@" not in out and cat is not None, out


def test_email_delimiters_kept() -> None:
    assert sweep("b'maria@x.com'") == ("b'<email>'", 1)
    assert sweep("{'email': 'a@b.co'}") == ("{'email': '<email>'}", 1)
    assert sweep("key=a@b.co") == ("key=<email>", 1)


@pytest.mark.parametrize(
    "value",
    [
        "Refund for Maria Garcia",
        "user Maria Garcia not found",
        "Dr. Maria Garcia",
        "Garcia, Maria",
        "MARIA GARCIA",
        "John Smith-Jones",
        "Ronald McDonald",
        "O'Brien Patrick",
        "{'name': 'Maria Garcia'}",
        "a b c Maria Garcia d e",
    ],
)
def test_names_masked(value: str) -> None:
    out, cat = mask_value(value)
    assert "<name>" in out and cat == "name", out
    for leaked in ("Maria", "Garcia", "Smith", "Jones", "McDonald", "Brien", "Patrick", "Ronald"):
        assert leaked not in out, out


def test_name_masking_keeps_context() -> None:
    assert mask_value("Refund for Maria Garcia") == ("Refund for <name>", "name")


@pytest.mark.parametrize(
    "value",
    [
        "Spring Sale",
        "Paid",
        "Blue Widget Pro",
        "Order Shipped",
        "Black Friday",
        "Premium Plan",
        "Credit Card",
        "Summer Collection",
        "Standard Shipping",
        "Customer Support",
        "PAID",
        "ORDER SHIPPED",
        "Pending Review",
        "North America",
        "ORD-1001",
        "SKU-AB-12",
    ],
)
def test_ordinary_values_pass(value: str) -> None:
    assert mask_value(value) == (value, None)


@pytest.mark.parametrize(
    "value",
    ["Spring 2026", "String 5", "Offering 3", "Monitoring 2", "10 Place", "2 Way", "Abstr. 5"],
)
def test_address_false_positives_gone(value: str) -> None:
    assert mask_value(value) == (value, None)


@pytest.mark.parametrize("value", ["Baker Street 12", "flat 3, 12 baker st", "Hauptstraße 12"])
def test_addresses_masked(value: str) -> None:
    assert mask_value(value) == ("<address>", "address")


def test_european_international_phone() -> None:
    assert mask_value("00491701234567") == ("<phone>", "phone")
    assert sweep("call 00491701234567 now") == ("call <phone> now", 1)
    assert mask_value("0049 170 1234567") == ("<phone>", "phone")


def test_lowercase_iban() -> None:
    assert mask_value("gb82west12345698765432") == ("<iban>", "iban")
    assert sweep("pay gb82west12345698765432 today") == ("pay <iban> today", 1)


@pytest.mark.parametrize(
    "value",
    [
        "Bearer Zx9Qm2Lp8Rt4Vb7Nc1Kd6Hs3Jf5aa",
        "api_key=Zx9Qm2Lp8Rt4Vb7Nc1Kd6Hs3Jf5aa",
        "it failed, key=sk_test_Zx9Qm2Lp8Rt4Vb7Nc1 please retry",
        "Authorization: Bearer Zx9Qm2Lp8Rt4Vb7Nc1Kd6Hs3Jf5aa",
        "retry with password: hunter2hunter2",
        "sk_live_Zx9Qm2Lp8Rt4Vb7Nc1Kd6",
    ],
)
def test_secrets_masked(value: str) -> None:
    out, cat = mask_value(value)
    assert cat == "secret" and "Zx9" not in out and "hunter2" not in out, out
    assert "<secret>" in out


def test_secret_rule_leaves_schema_prose_and_is_idempotent() -> None:
    prose = "primary key references orders; token count is low; foreign key customer_id"
    assert sweep(prose) == (prose, 0)
    once, n = sweep("api_key=Zx9Qm2Lp8Rt4Vb7Nc1Kd6Hs3Jf5aa")
    assert (once, n) == ("api_key=<secret>", 1)
    assert sweep(once) == (once, 0)


def test_previous_behaviours_survive() -> None:
    for v, tok in [
        ("+1 (415) 555-2671", "<phone>"),
        ("jane.doe+tag@Example.COM", "<email>"),
        ("a@b.com, c@d.org", "<email>"),
        ("2001:db8::ff00:42:8329", "<ip>"),
        ("4111 1111 1111 1111", "<card>"),
        ("GB82 WEST 1234 5698 7654 32", "<iban>"),
        ("123e4567-e89b-12d3-a456-426614174000", "<uuid>"),
    ]:
        assert mask_value(v)[0] == tok, v
    for v in [
        "weekly",
        "paid",
        "2026-11-24",
        "2026-11-24 10:30:00",
        "3.14159",
        "ORD-1001",
        "SKU-12345",
        "https://example.com/x/1",
    ]:
        assert mask_value(v) == (v, None), v


@pytest.mark.parametrize(
    "prose",
    [
        "primary key: customer_id",
        "foreign key: order_id references orders(id)",
        "partition key: event_date",
        "sort key = created_at",
        "key=customer_id",
        "token: refreshing",
        "secret: required",
        "authorization: required_header",
        "Lookup key: SKU-1234",
        "password: required_field_here",
        "uses bearer authentication",
        "token=refresh_required",
    ],
)
def test_kv_rule_leaves_schema_prose(prose: str) -> None:
    assert sweep(prose) == (prose, 0)


@pytest.mark.parametrize(
    "secret",
    [
        "api_key=Zx9Qm2Lp8Rt4Vb7Nc1Kd6Hs3Jf5aa",
        "key=sk_test_Zx9Qm2Lp8Rt4Vb7Nc1",
        "Bearer Zx9Qm2Lp8Rt4Vb7Nc1Kd6Hs3Jf5aa",
        "token: ghp_Zx9Qm2Lp8Rt4Vb7Nc1Kd6",
        "secret=AKIAZx9Qm2Lp8Rt4Vb7",
        "password: hunter2hunter2",
        "api-key: wJalrXUtnFEMIK7MDENGbPxRfiCY",
    ],
)
def test_kv_rule_still_masks_credentials(secret: str) -> None:
    out, n = sweep(secret)
    assert n >= 1 and "<secret>" in out and "Zx9" not in out and "hunter2" not in out, out


# Fixtures are assembled at runtime so secret scanners do not flag this file.
_AWS = "AK" + "IA" + "ZX9QM2LP8RT4VB7N"
_GH = "gh" + "p_" + "Zx9Qm2Lp8Rt4Vb7Nc1Kd6Hs3Jf5Wy0Ue4Ra7Bc"
_SLACK = "xo" + "xb-" + "1234567890-Zx9Qm2Lp8Rt4Vb7Nc1"
_GOOG = "AI" + "za" + "Sy" + "Zx9Qm2Lp8Rt4Vb7Nc1Kd6Hs3Jf5Wy0Ue4"


@pytest.mark.parametrize("secret", [_AWS, _GH, _SLACK, _GOOG, "AS" + "IA" + "ZX9QM2LP8RT4VB7N"])
def test_bare_credentials_masked_in_prose(secret: str) -> None:
    assert len(secret) > 15
    text, n = sweep(f"leaked {secret} here, ok")
    assert text == "leaked <secret> here, ok" and n == 1, text
    assert sweep(f"({secret})")[0] == "(<secret>)"
    assert sweep(text) == (text, 0)
    assert mask_value(secret)[0] == "<secret>"


@pytest.mark.parametrize(
    "prose",
    [
        "ghost_town",
        "the akia river",
        "akiaiosfodnn7example",
        "xox",
        "xoxo hugs",
        "ghp_short",
        "AKIA short",
        "AKIAZX9QM2LP8RT4VB7NX",
        "AIza too short",
        "ghp is a prefix",
    ],
)
def test_credential_lookalikes_pass(prose: str) -> None:
    assert sweep(prose) == (prose, 0)


@pytest.mark.parametrize(
    ("value", "masked"),
    [
        ("Bob Smith", "<name>"),
        ("<Bob Smith>", "<<name>>"),
        ("Mary Brown", "<name>"),
        ("James Miller", "<name>"),
        ("Alice Johnson's table", "<name>'s table"),
        ("John Smith's IBAN", "<name>'s IBAN"),
        ("Alice Johnson\u2019s table", "<name>\u2019s table"),
        ("ask Robert Williams's team", "ask <name>'s team"),
    ],
)
def test_common_and_possessive_names_are_masked(value: str, masked: str) -> None:
    assert mask_value(value) == (masked, "name")


def test_common_word_names_are_in_the_list() -> None:
    from schemaguru.pii.detectors import name_list

    names = name_list()
    for n in ("smith", "brown", "miller", "bob", "james", "wilson", "taylor", "white", "king"):
        assert n in names, n
