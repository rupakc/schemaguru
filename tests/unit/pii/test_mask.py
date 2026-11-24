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


def test_mask_free_text_lives_in_pii_and_masks_shapes() -> None:
    from schemaguru.pii import mask_free_text

    text, hits = mask_free_text("Binder Error: value 'alice@example.com' is not valid")
    assert "alice@example.com" not in text and "<email>" in text and hits >= 1


# ---- sql_template: blank literals with the shared agreed lexer, fail closed -------------------

import pytest  # noqa: E402

from schemaguru.pii import scrub_tree, sql_template  # noqa: E402


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1 WHERE x = E'it\\'s secret@x.com'",  # backslash escape (E-string)
        "SELECT 1 WHERE x = r'ab\\'c' AND y = 'secret@x.com'",  # raw-string backslash
        "SELECT $$alice@example.com$$",  # dollar quoting
        "SELECT $tag$alice@example.com$tag$",
    ],
)
def test_sql_template_never_leaks_through_dialect_quoting(sql: str) -> None:
    out = sql_template(sql)
    assert out is None or ("@" not in out and "secret" not in out and "alice" not in out), out


def test_sql_template_blanks_plain_literals_and_keeps_identifiers() -> None:
    sql = "SELECT \"email\", 'O''Brien' FROM t WHERE e IN ('alice@example.com', N'bob')"
    assert sql_template(sql) == 'SELECT "email", ? FROM t WHERE e IN (?, N?)'


def test_sql_template_masks_pii_shaped_double_quoted_spans() -> None:
    out = sql_template('SELECT 1 WHERE email = "alice@example.com"')
    assert out == "SELECT 1 WHERE email = ?"


def test_sql_template_ambiguous_or_unterminated_is_none() -> None:
    assert sql_template("SELECT 'unterminated alice@example.com") is None


def test_scrub_tree_replacement_reaches_nested_params() -> None:
    tree = ["x@y.com", ["x@y.com"], {"a": "x@y.com", "n": 5}]
    assert scrub_tree(tree, "none", replacement="?") == ["?", ["?"], {"a": "?", "n": 5}]


@pytest.mark.parametrize("ident", ["Unit Price", "Order Items", "John Smith", "Main Street 5"])
def test_sql_template_keeps_identifiers_that_only_look_like_names(ident: str) -> None:
    sql = f'SELECT "{ident}" FROM "Order Items"'
    assert sql_template(sql) == sql


@pytest.mark.parametrize("value", ["alice@example.com", "+49 170 1234567"])
def test_sql_template_replaces_shape_hits_in_double_quoted_spans(value: str) -> None:
    assert sql_template(f'SELECT 1 WHERE c = "{value}"') == "SELECT 1 WHERE c = ?"
