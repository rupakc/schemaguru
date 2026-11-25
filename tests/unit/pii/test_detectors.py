from __future__ import annotations

import hashlib

import pytest

from schemaguru.pii import detectors as D
from schemaguru.pii.detectors import PIIFinding, classify_column, register_detector


def by(name: str) -> D.Detector:
    return next(d for d in D.all_detectors() if d.name == name)


TRUTH: dict[str, tuple[list[str], list[str]]] = {
    "email": (
        [
            "a@b.co",
            "jane.doe+tag@example.com",
            "a@b.com, c@d.org",
            "x@sub.example.co.uk",
            "a@b.com;c@d.com",
            "  padded@example.com  ",
        ],
        ["plain", "a@b", "@example.com", "two words@example.com", "a@@b.com", "12345"],
    ),
    "phone": (
        [
            "+4915123456789",
            "+1 (415) 555-2671",
            "+44 20 7946 0958",
            "030 1234567",
            "(030) 123456",
            "415-555-2671",
            "0301234567",
        ],
        ["12345", "2024-01-15", "192.168.1.1", "3.14159", "123456789012345678", "abc-def-ghij"],
    ),
    "iban": (
        [
            "DE89370400440532013000",
            "GB82 WEST 1234 5698 7654 32",
            "FR1420041010050500013M02606",
            "NL91ABNA0417164300",
            "ES9121000418450200051332",
        ],
        ["DE89370400440532013001", "GB82WEST12345698765433", "hello world", "DE12", "XX00"],
    ),
    "card": (
        [
            "4111111111111111",
            "4111 1111 1111 1111",
            "5500-0000-0000-0004",
            "378282246310005",
            "6011111111111117",
        ],
        ["4111111111111112", "1111111111111111", "1234", "12345678", "abcdefghijklmnop"],
    ),
    "ipv4": (
        ["192.168.0.1", "10.0.0.255", "8.8.8.8", "255.255.255.255", "127.0.0.1"],
        ["256.1.1.1", "1.2.3", "1.2.3.4.5", "a.b.c.d", "10.0.0"],
    ),
    "ipv6": (
        [
            "::1",
            "2001:db8::ff00:42:8329",
            "fe80::1",
            "2001:0db8:85a3:0000:0000:8a2e:0370:7334",
            "::ffff:192.0.2.1",
        ],
        ["10:30:45", "12:30", "::", "abc", "gggg::1"],
    ),
    "uuid": (
        [
            "123e4567-e89b-12d3-a456-426614174000",
            "00000000-0000-0000-0000-000000000000",
            "A1B2C3D4-E5F6-7890-ABCD-EF1234567890",
            "f47ac10b-58cc-4372-a567-0e02b2c3d479",
            "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
        ],
        [
            "123e4567-e89b-12d3-a456-42661417400",
            "not-a-uuid",
            "123e4567e89b12d3a456426614174000",
            "123e4567-e89b-12d3-a456",
            "zzzzzzzz-zzzz-zzzz-zzzz-zzzzzzzzzzzz",
        ],
    ),
    "hex": (
        [
            "d41d8cd98f00b204e9800998ecf8427e",
            "da39a3ee5e6b4b0d3255bfef95601890afd80709",
            "0123456789abcdef0123",
            "ABCDEF0123456789ABCDEF",
            "e3b0c44298fc1c149afbf4c8996fb924",
        ],
        [
            "0123456789abcdef",
            "deadbeef",
            "12345678901234567890",
            "xyz0123456789abcdef0123",
            "d41d8cd98f00b204e980"[:-1],
        ],
    ),
    "secret": (
        [
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r",
            "eyJhbGciOiJub25lIn0.eyJhIjoxfQ.",
            "x9Qz7LmT2vKb8WnR4pYc1HdJ6sGa",
            "Qm4Tz8Lk2Vb7Wn1Rp9Yc3HdJ5sGa6",
            "pT3/Zk8Lm2Vb7Wn1Rq9Yc4HdJ5sGa6x",
            "https://example.com/a1b2c3d4e5f6g7h8i9j0k1l2",  # a token inside a URL (0.2.0)
        ],
        [
            "internationalization",
            "Supercalifragilisticexpialidocious",
            "pneumonoultramicroscopicsilicovolcanoconiosis",
            "SomeVeryLongCamelCaseIdentifierName",
            "123e4567-e89b-12d3-a456-426614174000",
            "d41d8cd98f00b204e9800998ecf8427e",
            "https://example.com/orders/report.pdf",
            "a.b.c",
            "short1x",
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaa1",
        ],
    ),
    "address": (
        [
            "221B Baker Street",
            "10 Downing Street, London",
            "42 Wallaby Way",
            "Hauptstraße 12",
            "Muster Str. 5a",
            "Musterstr. 5a",
            "Baker Street 12",
            "flat 3, 12 baker st",
            "1600 Pennsylvania Avenue",
        ],
        [
            "Order shipped",
            "12 apples",
            "Pending",
            "Street",
            "42",
            "Spring 2026",
            "String 5",
            "Offering 3",
            "Monitoring 2",
            "10 Place",
            "2 Way",
            "Abstr. 5",
        ],
    ),
    "person_name": (
        [
            "Maria Garcia",
            "John Smith",
            "Hiroshi Tanaka",
            "Anna Maria Schmidt",
            "Olga Ivanova",
            "MARIA GARCIA",
            "Dr. Maria Garcia",
            "Garcia, Maria",
            "John Smith-Jones",
            "Ronald McDonald",
            "O'Brien Patrick",
            "john smith",  # listed names in lower case (0.2.0)
        ],
        [
            "Order Shipped",
            "Pending Review",
            "order shipped",
            "Maria",
            "Total Amount Due Now Please",
        ],
    ),
    "free_text": (
        [
            "This is a long note that goes past the sixty character limit for sure, yes it does.",
            "word " * 13,
            "The customer asked for a refund because the parcel arrived late and damaged.",
            "Multiple words here, " * 4,
            "Lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod",
        ],
        ["short text", "x" * 80, "a" * 61, "one two three", "no"],
    ),
}


@pytest.mark.parametrize("name", sorted(TRUTH))
def test_positive(name: str) -> None:
    pos, _ = TRUTH[name]
    assert len(pos) >= 3
    for v in pos:
        assert by(name).matches(v), (name, v)


@pytest.mark.parametrize("name", sorted(TRUTH))
def test_negative(name: str) -> None:
    _, neg = TRUTH[name]
    assert len(neg) >= 5
    for v in neg:
        assert not by(name).matches(v), (name, v)


def test_truth_tables_have_enough_cases() -> None:
    # spec: >=5 positive and >=5 negative cases for every detector
    for name, (pos, neg) in TRUTH.items():
        assert len(pos) >= 5 and len(neg) >= 5, name


def test_hex_16_not_masked() -> None:
    assert not by("hex").matches("0123456789abcdef")
    assert by("hex").matches("0123456789abcdef0123")


def test_iban_checksum() -> None:
    assert D.iban_ok("DE89370400440532013000")
    assert not D.iban_ok("DE89370400440532013001")


def test_luhn() -> None:
    assert D.luhn_ok("4111111111111111")
    assert not D.luhn_ok("4111111111111112")


def test_secret_does_not_flag_words_or_uuids() -> None:
    s = by("secret")
    assert not s.matches("internationalizationandlocalizationofsoftware")
    assert not s.matches("123e4567-e89b-12d3-a456-426614174000")
    assert not s.matches("d41d8cd98f00b204e9800998ecf8427e")


def test_name_list_bundled() -> None:
    names = D.name_list()
    assert len(names) >= 2000
    assert all(n == n.lower() for n in names)
    assert "maria" in names and "garcia" in names


def test_common_english_phrases_are_not_names() -> None:
    n = by("person_name")
    for phrase in [
        "Order Shipped",
        "Payment Received",
        "Credit Card",
        "Customer Support",
        "Product Catalog",
        "Account Balance",
        "Shipping Address",
        "Session Start",
    ]:
        assert not n.matches(phrase), phrase


def test_person_name_opt_out() -> None:
    try:
        register_detector(D.PersonNameDetector(enabled=False), replace=True)
        assert not by("person_name").matches("Maria Garcia")
    finally:
        D.reset_detectors()
    assert by("person_name").matches("Maria Garcia")


def test_register_detector_duplicate_and_replace() -> None:
    class Plate:
        name = "plate"
        category = "plate"

        def matches(self, value: str) -> bool:
            return value.startswith("PLT-")

    try:
        register_detector(Plate())
        with pytest.raises(ValueError):
            register_detector(Plate())
        register_detector(Plate(), replace=True)
        assert by("plate").matches("PLT-1")
    finally:
        D.reset_detectors()
    assert all(d.name != "plate" for d in D.all_detectors())


def test_name_alone_never_classifies() -> None:
    f = classify_column([("daily", 60), ("weekly", 40)], 2, "email_frequency")
    assert f is None
    assert classify_column([("yes", 5)], 1, "phone_number") is None


def test_shape_classifies_regardless_of_name() -> None:
    f = classify_column([(f"user{i}@example.com", 1) for i in range(50)], 50, "contact")
    assert f == PIIFinding("email", "email", f.reason)
    assert "email" in f.reason


def test_name_hint_lowers_threshold() -> None:
    vals = [("a@b.co", 25), ("none", 75)]
    assert classify_column(vals, 30, "other") is None
    hinted = classify_column(vals, 30, "contact_email")
    assert hinted is not None and hinted.category == "email"


def test_low_cardinality_hash_flagged_review() -> None:
    hashes = [(hashlib.md5(str(i).encode()).hexdigest(), 10) for i in range(5)]
    f = classify_column(hashes, 5, "kind")
    assert f is not None and f.category == "review" and f.detector == "hex"
    assert "5 distinct" in f.reason
    # many distinct hashes are real identifiers, unknown cardinality skips the trap
    assert classify_column(hashes, 5000, "kind") == PIIFinding("hex", "hex", mk(hashes))
    unknown = classify_column(hashes, None, "kind")
    assert unknown is not None and unknown.category == "hex"


def mk(values: list[tuple[str, int]]) -> str:
    return "100% of observed values match the hex shape"


def test_blank_values_ignored() -> None:
    assert classify_column([("", 10), ("  ", 5)], 2, "email") is None
    assert classify_column([], None, "x") is None
