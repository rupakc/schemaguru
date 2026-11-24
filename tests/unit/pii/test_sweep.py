from __future__ import annotations

from schemaguru.pii import sweep


def test_sweep_catches_embedded_pii() -> None:
    text, n = sweep("customer jane.doe@example.com asked about order 5")
    assert text == "customer <email> asked about order 5" and n == 1
    err = "ValueError: invalid value 'bob@corp.example' for column email (row 3)"
    out, n = sweep(err)
    assert "bob@corp.example" not in out and "<email>" in out and n == 1


def test_sweep_counts_multiple_kinds() -> None:
    payload = (
        "ip 10.1.2.3, card 4111 1111 1111 1111, iban DE89370400440532013000, "
        "id 123e4567-e89b-12d3-a456-426614174000, hash d41d8cd98f00b204e9800998ecf8427e, "
        "call +4915123456789"
    )
    out, n = sweep(payload)
    for t in ("<ip>", "<card>", "<iban>", "<uuid>", "<hex32>", "<phone>"):
        assert t in out, (t, out)
    assert n == 6


def test_sweep_leaves_ordinary_text() -> None:
    s = "orders has 1,234,567 rows; created 2024-01-15 10:30:00; 16 hex 0123456789abcdef; v1.2"
    assert sweep(s) == (s, 0)


def test_sweep_idempotent_and_counts_zero_second_time() -> None:
    out, n = sweep("a@b.co and c@d.org")
    assert n == 2
    assert sweep(out) == (out, 0)


def test_sweep_bad_checksums_untouched() -> None:
    s = "GB82WEST12345698765433 and 4111111111111112"
    assert sweep(s) == (s, 0)


def test_sweep_jwt() -> None:
    tok = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r"
    out, n = sweep(f"token={tok}")
    assert out == "token=<secret>" and n == 1


def test_sweep_trailing_punctuation() -> None:
    assert sweep("mail me at a@b.co.") == ("mail me at <email>.", 1)
