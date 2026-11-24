from __future__ import annotations

import pytest

from schemaguru.pii import mask_value


@pytest.mark.parametrize(
    ("value", "text", "cat"),
    [
        ("jane@example.com", "<email>", "email"),
        ("a@b.com, c@d.org", "<email>", "email"),
        ("+4915123456789", "<phone>", "phone"),
        ("DE89370400440532013000", "<iban>", "iban"),
        ("4111 1111 1111 1111", "<card>", "card"),
        ("10.1.2.3", "<ip>", "ip"),
        ("::1", "<ip>", "ip"),
        ("123e4567-e89b-12d3-a456-426614174000", "<uuid>", "uuid"),
        ("d41d8cd98f00b204e9800998ecf8427e", "<hex32>", "hex"),
        ("da39a3ee5e6b4b0d3255bfef95601890afd80709", "<hex40>", "hex"),
        ("Maria Garcia", "<name>", "name"),
        ("221B Baker Street", "<address>", "address"),
        ("x" * 3 + " " + "y" * 70, "<free_text len=74>", "free_text"),
    ],
)
def test_shapes(value: str, text: str, cat: str) -> None:
    assert mask_value(value) == (text, cat)


def test_blank_is_null() -> None:
    for v in (None, "", "   ", "\n\t"):
        assert mask_value(v) == ("", None)


def test_plain_values_pass_through() -> None:
    assert mask_value("shipped") == ("shipped", None)
    assert mask_value(42) == ("42", None)
    assert mask_value(0.5) == ("0.5", None)


def test_non_string_card_number_masked() -> None:
    assert mask_value(4111111111111111) == ("<card>", "card")


def test_embedded_email_masked_in_short_text() -> None:
    text, cat = mask_value("contact jane@example.com now")
    assert text == "contact <email> now" and cat == "email"


def test_mask_idempotent_examples() -> None:
    for v in [
        "jane@example.com",
        "d41d8cd98f00b204e9800998ecf8427e",
        "hi " + "z " * 40,
        "contact a@b.co now",
    ]:
        once = mask_value(v)[0]
        assert mask_value(once)[0] == once
