from __future__ import annotations

import time
from collections.abc import Callable

import pytest

from schemaguru.pii import all_detectors, mask_free_text, mask_value, sweep
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
    "github_pat_long": "git" + "hub_pat_" + "a" * N,
    "xapp-long": "xa" + "pp-" + "a" * N + "!",
    # 0.2.0 rules: colon passwords, in-text addresses, cues, lower-case names, paths, glue
    "pwd:": "pwd:" * (N // 4),
    'pwd="': 'pwd="' + "a" * N,
    "1 Elm ": "1 Elm " * (N // 6),
    "Elm St ": "Elm St " * (N // 7),
    "strasse": "a" * N + "strasse 1",
    "Mr ": "Mr " * (N // 3),
    "by A ": "by A " * (N // 5),
    "maria ": "maria " * (N // 6),
    "M. ": "M. " * (N // 3),
    "path": "/" + "a1b2c3d4/" * (N // 9),
    "_ghp_": "_" + "ghp_" * (N // 4),
    # fix round 1: quoted credential values, segmented keys under a path
    'token": "': '{"token": "' + "a1" * (N // 2),
    '"token":"': '"token":"' * (N // 9),
    "password: ' '": "password: '" + " " * N + "x",
    "segkey path": "/" + "ab1-" * (N // 4),
    "hex path": "/" + "9f86d081884c7d659a2f/" * (N // 21),
}


SMALL_N = 12_500
# Linear work doubles-to-quadruples when the input grows 4x; quadratic is 16x.
# The ratio, not the wall clock, is what catches backtracking, so it stays
# meaningful under coverage tracing, which slows every line by a similar factor.
MAX_SCALING_RATIO = 8.0
# Generous absolute ceiling: catastrophic backtracking takes seconds, not 2 s.
ABS_CEILING_S = 2.0
# Below this the small run is dominated by timer and scheduler noise (a Windows runner measured
# 0.04 s for a linear 50k run against a 0.0002 s small run: ratio 8.2 from noise alone).
NOISE_FLOOR_S = 0.01


def _best(fn: Callable[[str], object], arg: str) -> float:
    best = 1e9
    for _ in range(3):
        t = time.perf_counter()
        fn(arg)
        best = min(best, time.perf_counter() - t)
    return best


def _assert_scales_linearly(fn: Callable[[str], object], text: str, label: object) -> None:
    """Assert fn scales linearly from the 12.5k to the 50k input (a ratio, so it holds on a slow
    or loaded runner; the absolute ceiling is the ``benchmark`` test below)."""
    t_small = _best(fn, text[:SMALL_N])
    t_large = _best(fn, text)
    ratio = t_large / max(t_small, NOISE_FLOOR_S)
    assert ratio < MAX_SCALING_RATIO, (label, t_small, t_large, round(ratio, 2))


@pytest.mark.benchmark
@pytest.mark.parametrize("key", sorted(ADVERSARIAL))
def test_benchmark_mask_and_sweep_under_ceiling(key: str) -> None:
    text = ADVERSARIAL[key]
    for fn in (mask_value, sweep):
        assert _best(fn, text) < ABS_CEILING_S, (key, fn.__name__)


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
        "api_key: required_field_here",
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
_GH_PAT = "git" + "hub_pat_" + "11AZX9QM2" + "0" * 13 + "_Zx9Qm2Lp8Rt4Vb7Nc1Kd6Hs3Jf5Wy0Ue4Ra7Bc"
_SLACK_APP = "xa" + "pp-" + "1-A0ZX9QM2LP8-1234567890-" + "a1b2c3d4" * 4


@pytest.mark.parametrize(
    "secret",
    [_AWS, _GH, _SLACK, _GOOG, "AS" + "IA" + "ZX9QM2LP8RT4VB7N", _GH_PAT, _SLACK_APP],
)
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


# --- final fix wave: a short password=, and keys glued to a word ------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "password=hunter2",
        "password=a",
        "login failed (passwd=x9)",
        "db_password=abc",
        "x_password=hunter2",
        "x_api_key=Zx9Qm2Lp8Rt4Vb7Nc1Kd6Hs3Jf5aa",
        "conn_secret=AKIAZx9Qm2Lp8Rt4Vb7",
        "MY-TOKEN=ghp_Zx9Qm2Lp8Rt4Vb7Nc1Kd6",
    ],
)
def test_short_and_glued_credentials_are_masked(text: str) -> None:
    out, n = sweep(text)
    assert n >= 1 and "<secret>" in out, out
    for leaked in ("hunter2", "abc", "x9", "Zx9", "AKIA", "ghp_"):
        assert leaked not in out.split("=", 1)[1], out
    assert sweep(out) == (out, 0)  # idempotent


def test_glued_key_keeps_its_prefix_and_prose_stays() -> None:
    assert sweep("db_password=abc")[0] == "db_password=<secret>"
    # negative controls: schema prose and empty values are untouched
    for prose in ("customer_key = 12345678", "token: required_field_here", "password="):
        assert sweep(prose) == (prose, 0), prose


# --- 0.2.0 hardening (ledger A1): red-first tests, one per item --------------------------------


def _no_leak(out: str, *words: str) -> None:
    for w in words:
        assert w not in out, (w, out)


def test_comma_joined_name_masked() -> None:  # A1.1
    out, n = mask_free_text("call Garcia,Maria today")
    assert "<name>" in out and n >= 1, out
    _no_leak(out, "Garcia", "Maria")
    assert out == "call <name> today"
    assert mask_value("Garcia,Maria") == ("<name>", "name")


def test_initial_surname_masked() -> None:  # A1.2
    assert mask_value("M. Garcia") == ("<name>", "name")
    assert mask_free_text("refund for M. Garcia today") == ("refund for <name> today", 1)
    assert mask_free_text("Maria J. Garcia called")[0] == "<name> called"


def test_lowercase_name_masked() -> None:  # A1.3
    assert mask_value("maria garcia") == ("<name>", "name")
    assert mask_free_text("refund for maria garcia today") == ("refund for <name> today", 1)
    out = mask_free_text("Contact Maria garcia now")[0]
    _no_leak(out, "Maria", "garcia")
    # a lowercase word that is not in the list ends the run: ordinary prose stays
    for prose in ("the order was paid", "status is pending review", "maria"):
        assert mask_free_text(prose) == (prose, 0), prose


@pytest.mark.parametrize(
    ("text", "masked"),
    [
        ("customer Zorblat Quenvik called", "customer <name> called"),
        ("contact Zorblat Quenvik", "contact <name>"),
        ("Contact Zorblat Quenvik", "Contact <name>"),
        ("approved by Zorblat Quenvik", "approved by <name>"),
        ("a note from Zorblat Quenvik", "a note from <name>"),
        ("Mr Zorblat Quenvik", "Mr <name>"),
        ("Ms. Zorblat Quenvik", "Ms. <name>"),
        ("Dr Zorblat Quenvik", "Dr <name>"),
        ("Dr. Zorblat Quenvik", "Dr. <name>"),
    ],
)
def test_cued_unlisted_name_masked(text: str, masked: str) -> None:  # A1.4
    assert mask_free_text(text)[0] == masked
    assert mask_value(text)[0] == masked


def test_uncued_unlisted_pair_is_kept() -> None:  # A1.4 negative control
    for prose in ("Zorblat Quenvik", "Spring Sale", "Order Shipped", "sort by date"):
        assert mask_free_text(prose) == (prose, 0), prose


def test_surname_possessive_masked() -> None:  # A1.5
    assert mask_value("Garcia's order") == ("<name>'s order", "name")
    assert mask_free_text("see Garcia\u2019s order") == ("see <name>\u2019s order", 1)
    # a possessive of an unlisted word stays
    assert mask_free_text("the Widget's colour") == ("the Widget's colour", 0)


def test_address_in_sentence_masked() -> None:  # A1.6
    text = "ship to 42 Elm Street, Springfield today"
    want = "ship to <address> today"
    assert mask_free_text(text) == (want, 1)
    assert sweep(text) == (want, 1)
    assert mask_value(text) == (want, "address")
    for t, w in [
        ("left at Baker Street 12 yesterday", "left at <address> yesterday"),
        ("send to 12 baker street today", "send to <address> today"),
        ("moved to Hauptstraße 12 in May", "moved to <address> in May"),
    ]:
        assert sweep(t) == (w, 1), t
    for prose in ("updated 2 times via the API", "Rank 1 takes first place", "10 Place"):
        assert sweep(prose) == (prose, 0), prose


@pytest.mark.parametrize(
    ("text", "masked"),
    [
        ("password: hunter2", "password: <secret>"),
        ("pwd: abc", "pwd: <secret>"),
        ("pwd=abc", "pwd=<secret>"),
        ("login failed, password: a1", "login failed, password: <secret>"),
        ("db_pwd: x9 retry", "db_pwd: <secret> retry"),
        ('{"password": "hunter2"}', '{"password": "<secret>"}'),
        ("pwd='abc'", "pwd='<secret>'"),
        # recall over precision: any value after "password:" is masked, words included
        ("password: required_field_here", "password: <secret>"),
    ],
)
def test_short_colon_credential_masked(text: str, masked: str) -> None:  # A1.7 colon forms
    assert sweep(text) == (masked, 1)
    assert sweep(masked) == (masked, 0)


_GLUED_SK = "x_" + "sk_" + "live_" + "Zx9Qm2Lp8Rt4Vb7Nc1Kd6"
_GLUED_GH = "token_" + _GH


@pytest.mark.parametrize("glued", [_GLUED_SK, _GLUED_GH, "my_" + _AWS, "cfg_" + _SLACK])
def test_glued_credential_masked(glued: str) -> None:  # A1.8
    out, n = sweep(f"leaked {glued} in the log")
    assert n == 1 and "<secret>" in out, out
    _no_leak(out, "Zx9", "ZX9")
    assert sweep(out) == (out, 0)


def test_glued_credential_lookalikes_pass() -> None:  # A1.8 negative control
    for prose in ("disk_test_results2026", "task_live_counter", "x_ghp_short"):
        assert sweep(prose) == (prose, 0), prose


def test_zero_padded_id_not_phone() -> None:  # A1.10
    for v in ("000123456", "0001234567", "000012345678", "00000001234567"):
        assert mask_value(v) == (v, None), v
        assert sweep(f"id {v} ok") == (f"id {v} ok", 0), v
    # recall: '+', separators and the 00 international prefix stay phones
    for v in ("+44 20 7946 0958", "000-123-4567", "0001 234 5678", "00491701234567"):
        assert mask_value(v)[1] == "phone", v


@pytest.mark.parametrize(
    ("path", "masked"),
    [
        ("/var/data/x.csv", "/var/data/x.csv"),
        ("s3://bucket/key", "s3://bucket/key"),
        ("/Users/x/iPhone/MyApp/a.csv", "/Users/x/iPhone/MyApp/a.csv"),
    ],
)
def test_path_not_secret(path: str, masked: str) -> None:  # A1.11: short paths stay
    out, cat = mask_value(path)
    assert out == masked and cat != "secret", out


@pytest.mark.parametrize(
    "path",
    [
        "/data/warehouse/2026/11/25/part-00017-snappy.parquet",
        "./exports/customers_2026-11-25_v3.csv",
        "s3://bucket/dt=2026-11-25/hour=13/part-0001.json",
        # A1.11 ruled out: a hex/UUID-keyed path is masked whole, never shown around the key
        "/9f86d081884c7d659a2feaa0c55ad015/k2m8z-p4w7r",
        "/var/data/9f86d081884c7d659a2feaa0c55ad015/x.csv",
        "/store/123e4567-e89b-12d3-a456-426614174000/report.csv",
        "/9f86d081884c7d659a2feaa0c55ad015/hoagn-ddmjx-kcycs-tnydu",
        "./cb3697231602ede56794929893c/wjgkqofvuitr",
    ],
)
def test_long_varied_path_stays_masked(path: str) -> None:  # A1.11 recall over precision
    assert mask_value(path) == ("<secret>", "secret")


def test_path_with_token_inside_is_masked() -> None:  # A1.11 recall
    for v in (
        "/srv/keys/Zx9Qm2Lp8Rt4Vb7Nc1Kd6Hs3Jf5/id",
        "s3://bucket/Zx9Qm2Lp8Rt4Vb7Nc1Kd6Hs3Jf5aa",
        "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "/" + _GH,
    ):
        out, cat = mask_value(v)
        assert cat == "secret" and "Zx9" not in out and "bPxR" not in out, (v, out)


def test_version_not_ip() -> None:  # A1.12
    for v in ("1.2.3", "v10.0.1", "release 1.2.3 out", "v1.2.3.4", "1.2.3.4.5"):
        assert mask_value(v) == (v, None), v
        assert sweep(v) == (v, 0), v
    # recall: a valid dotted quad is an address whatever word precedes it
    assert mask_value("10.0.0.12") == ("<ip>", "ip")
    assert sweep("release 2.10.3.1") == ("release <ip>", 1)


@pytest.mark.parametrize(
    "text",
    [
        "password: varchar(255)",
        "password VARCHAR(255) NOT NULL",
        "pwd: text",
        "password: character varying(64)",
        "secret: numeric(10,2)",
    ],
)
def test_credential_key_with_sql_type_kept(text: str) -> None:  # A1.14
    assert sweep(text) == (text, 0)


def test_detectors_have_no_type_ignore() -> None:  # A1.16
    from pathlib import Path

    import schemaguru.pii.detectors as det

    source = Path(det.__file__).read_text("utf-8")
    assert "type: ignore" not in source
    assert "fn: Callable[[str], bool]" in source


# --- fix round 1: quoted credential values ----------------------------------------------------


@pytest.mark.parametrize(
    ("text", "masked"),
    [
        ('password="hunter2long"', 'password="<secret>"'),
        ('passwd="hunter2long"', 'passwd="<secret>"'),
        ("password='hunter2long' ok", "password='<secret>' ok"),
    ],
)
def test_quoted_password_keeps_both_quotes(text: str, masked: str) -> None:
    assert sweep(text) == (masked, 1)
    assert sweep(masked) == (masked, 0)


@pytest.mark.parametrize(
    ("text", "masked"),
    [
        ('{"token": "Zx9Qm2Lp8Rt4Vb7Nc1Kd6Hs3Jf5aa"}', '{"token": "<secret>"}'),
        ('{"secret": "Zx9Qm2Lp8Rt4Vb7Nc1Kd6Hs3Jf5aa"}', '{"secret": "<secret>"}'),
        ('{"api_key": "abcd1234efgh5678ijkl"}', '{"api_key": "<secret>"}'),
        ('{"secret":"hunter2hunter2x9"}', '{"secret":"<secret>"}'),
        ("{'access_key': 'Zx9Qm2Lp8Rt4Vb7Nc1Kd6'}", "{'access_key': '<secret>'}"),
        ('{"authorization": "Bearer Zx9Qm2Lp8Rt4Vb7Nc1Kd6"}', '{"authorization": "<secret>"}'),
    ],
)
def test_json_quoted_credential_masked(text: str, masked: str) -> None:
    assert sweep(text) == (masked, 1)
    assert sweep(masked) == (masked, 0)


def test_json_quoted_schema_prose_kept() -> None:
    for prose in ('{"token": "refresh_required"}', '{"key": "customer_id"}'):
        assert sweep(prose) == (prose, 0), prose


def test_quoted_password_edge_values_masked() -> None:
    assert sweep('{"password": " hunter2"}') == ('{"password": "<secret>"}', 1)
    long_value = "a1" * 200  # 400 chars, past any scan bound
    out, n = sweep(f'{{"password": "{long_value}"}} tail')
    assert n == 1 and "a1a1" not in out, out
