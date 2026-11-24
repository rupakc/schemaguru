"""Shape detectors (spec 11).

A :class:`Detector` answers one question about one value: does the *whole* value have this
shape? Built-ins are listed in priority order in :func:`all_detectors`; the first match decides the
category when a value could be several things (a 32-hex string is ``hex``, not ``secret``).
"""

from __future__ import annotations

import base64
import binascii
import ipaddress
import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Protocol, runtime_checkable

# ---------------------------------------------------------------------------------------------
# Shared patterns (also used by the outbound sweep, which searches instead of full-matching).

_LABEL = r"(?:[^\W_]|-){1,63}"
EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]{1,64}@"
    rf"{_LABEL}(?:\.{_LABEL}){{0,8}}\.[^\W\d_]{{2,24}}"
)
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
HEX_RE = re.compile(r"[0-9a-fA-F]{20,}")
JWT_RE = re.compile(r"[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]*")
IPV4_RE = re.compile(r"(?:\d{1,3}\.){3}\d{1,3}")
IPV6_RE = re.compile(r"(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}")
IBAN_RE = re.compile(r"[A-Za-z]{2}\d{2}(?: ?[A-Za-z0-9]){11,30}")
CARD_RE = re.compile(r"(?:\d[ \-]?){12,18}\d")
E164_RE = re.compile(r"\+[1-9](?:[ .\-()]{0,2}\d){6,14}")
NATIONAL_RE = re.compile(
    r"(?:\(\d{2,5}\)[ .\-]?\d{2,8}(?:[ .\-]\d{2,5}){0,3}"
    r"|0\d{1,4}[ ./\-]\d{3,12}(?:[ .\-]\d{2,12}){0,5}"
    r"|\d{3}[ .\-]\d{3}[ .\-]\d{4}"
    r"|00[1-9](?:[ .\-()]{0,2}\d){7,14}"
    r"|0[1-9]\d{8,12})"
)

_SCHEME_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://")
_SECRET_CHARS = re.compile(r"[A-Za-z0-9_\-+/=.~]+")
_STREET = (
    r"street|st|road|rd|avenue|ave|lane|ln|drive|dr|boulevard|blvd|court|ct|way|place|pl|"
    r"square|terrace|close|crescent|str|stra(?:\u00df|ss)e|rue|via|calle"
)
_WORD = r"[^\W\d_][\w'\-]*"
_UNIT = r"(?:(?:flat|apt|apartment|unit|suite|room)\.?\s{0,2}\d{1,4}[a-z]?,\s{0,2})?"
ADDRESS_RES = (
    # "10 Downing Street, London", "flat 3, 12 baker st": number, 1-4 name words, street word
    re.compile(
        rf"{_UNIT}\d{{1,4}}[a-z]?\s{{1,3}}(?:{_WORD}\s{{1,3}}){{1,4}}(?:{_STREET})\b\.?(?:,.{{0,60}})?",
        re.I,
    ),
    # "Baker Street 12": name words, street word, house number
    re.compile(
        rf"(?:{_WORD}\s{{1,3}}){{1,4}}(?:{_STREET})\b\.?\s{{0,3}}\d{{1,4}}[a-z]?(?:,.{{0,60}})?",
        re.I,
    ),
    # glued German forms: "Hauptstra\u00dfe 12", "Musterstr. 5a"
    re.compile(
        r"[^\W\d_]{3,}(?:stra(?:\u00df|ss)e|weg|platz|allee|gasse)\s{0,3}\d{1,4}[a-z]?(?:,.{0,60})?",
        re.I,
    ),
    re.compile(r"[^\W\d_]{4,}str\.\s{0,3}\d{1,4}[a-z]?(?:,.{0,60})?", re.I),
    # "Main Road 5, 12345 Town"
    re.compile(rf"(?:{_WORD}\s{{1,3}}){{1,4}}\d{{1,4}}[a-z]?,\s{{0,2}}\d{{4,5}}\s{{1,3}}{_WORD}"),
)

FREE_TEXT_MIN = 61


def luhn_ok(digits: str) -> bool:
    if not digits.isdigit():
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def iban_ok(text: str) -> bool:
    s = text.replace(" ", "").upper()
    if not 15 <= len(s) <= 34 or not s.isalnum() or not s.isascii():
        return False
    if not (s[:2].isalpha() and s[:2].isupper() and s[2:4].isdigit()):
        return False
    rearranged = s[4:] + s[:4]
    number = "".join(str(int(c, 36)) for c in rearranged)
    return int(number) % 97 == 1


def card_ok(text: str) -> bool:
    digits = re.sub(r"[ \-]", "", text)
    if not 13 <= len(digits) <= 19 or len(set(digits)) == 1:
        return False
    return luhn_ok(digits)


def ipv4_ok(text: str) -> bool:
    try:
        ipaddress.IPv4Address(text)
    except ValueError:
        return False
    return True


def ipv6_ok(text: str) -> bool:
    if ":" not in text or not any(c in "0123456789abcdefABCDEF" for c in text):
        return False
    try:
        ipaddress.IPv6Address(text)
    except ValueError:
        return False
    return True


def phone_ok(text: str) -> bool:
    digits = re.sub(r"\D", "", text)
    return 7 <= len(digits) <= 15 if text.startswith("+") else 9 <= len(digits) <= 15


def jwt_ok(text: str) -> bool:
    parts = text.split(".")
    if len(parts) != 3:
        return False
    head = parts[0] + "=" * (-len(parts[0]) % 4)
    try:
        raw = base64.urlsafe_b64decode(head)
    except (binascii.Error, ValueError):
        return False
    return raw.startswith(b"{")


def hex_ok(text: str) -> bool:
    return HEX_RE.fullmatch(text) is not None and any(c in "abcdefABCDEF" for c in text)


def shannon(text: str) -> float:
    n = len(text)
    return -sum(c / n * math.log2(c / n) for c in Counter(text).values())


# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PIIFinding:
    """A column-level classification: ``category`` is ``"review"`` when a human must decide."""

    category: str
    detector: str | None
    reason: str


@runtime_checkable
class Detector(Protocol):
    name: str
    category: str

    def matches(self, value: str) -> bool: ...


class _Fn:
    """A detector backed by a predicate over the stripped value."""

    def __init__(self, name: str, category: str, fn: object) -> None:
        self.name = name
        self.category = category
        self._fn = fn

    def matches(self, value: str) -> bool:
        return bool(self._fn(value.strip()))  # type: ignore[operator]


def _email(v: str) -> bool:
    parts = [p for p in re.split(r"[,;\s]+", v) if p]
    return bool(parts) and all(EMAIL_RE.fullmatch(p) for p in parts)


def _phone(v: str) -> bool:
    if ipv4_ok(v):
        return False
    return (E164_RE.fullmatch(v) is not None or NATIONAL_RE.fullmatch(v) is not None) and phone_ok(
        v
    )


def _iban(v: str) -> bool:
    return IBAN_RE.fullmatch(v) is not None and iban_ok(v)


def _card(v: str) -> bool:
    return CARD_RE.fullmatch(v) is not None and card_ok(v)


def _secret(v: str) -> bool:
    if jwt_ok(v) and JWT_RE.fullmatch(v):
        return True
    if len(v) < 24 or not _SECRET_CHARS.fullmatch(v) or _SCHEME_RE.match(v):
        return False
    if UUID_RE.fullmatch(v) or hex_ok(v):
        return False
    if not (any(c.isdigit() for c in v) and any(c.isalpha() for c in v)):
        return False
    return shannon(v) >= 4.0


def _address(v: str) -> bool:
    if len(v) > 120 or len(v) < 5:
        return False
    return any(rx.fullmatch(v) for rx in ADDRESS_RES)


def _free_text(v: str) -> bool:
    return len(v) >= FREE_TEXT_MIN and " " in v


@lru_cache(maxsize=1)
def name_list() -> frozenset[str]:
    text = resources.files("schemaguru.pii").joinpath("data/names.txt").read_text("utf-8")
    return frozenset(line.strip() for line in text.splitlines() if line.strip())


_NAME_TOKEN = re.compile("[^\\W\\d_]+(?:['\u2019\\-][^\\W\\d_]+)*")
_NAME_SPLIT = re.compile("['\u2019\\-]")
_MC = re.compile(r"(?:Mc|Mac)[A-Z][a-z]+")
_TITLE = re.compile(r"(?:Dr|Mr|Mrs|Ms|Miss|Prof|Herr|Frau|Mme|Mlle|Sir)\.?\s+")


def _part_ok(p: str) -> bool:
    if not p[0].isupper():
        return False
    rest = p[1:]
    return rest == "" or rest.islower() or p.isupper() or _MC.fullmatch(p) is not None


def _is_name_token(tok: str) -> bool:
    return all(_part_ok(p) for p in _NAME_SPLIT.split(tok))


def _is_name_hit(tok: str) -> bool:
    low = tok.lower()
    keys = {low, _NAME_SPLIT.sub("", low), *_NAME_SPLIT.split(low)}
    for prefix in ("mac", "mc"):
        if low.startswith(prefix) and len(low) > len(prefix) + 2:
            keys.add(low[len(prefix) :])
    names = name_list()
    return any(k in names for k in keys)


def name_spans(text: str) -> list[tuple[int, int]]:
    """Spans of person names inside ``text``.

    A name is a run of 2-4 capitalised tokens with at least one hit in the name list.

    Tokens may be Title-case, ALL-CAPS, ``McX``/``MacX``, ``O'X`` or hyphenated; they are
    joined by whitespace or by ``", "`` (``"Garcia, Maria"``). A run longer than four tokens
    is masked in windows of one token either side of each name-list hit. Linear in len(text).
    """
    spans: list[tuple[int, int]] = []
    run: list[tuple[int, int, bool]] = []

    def flush() -> None:
        if len(run) >= 2 and any(h for _, _, h in run):
            if len(run) <= 4:
                spans.append((run[0][0], run[-1][1]))
            else:
                keep = [False] * len(run)
                for i, (_, _, h) in enumerate(run):
                    if h:
                        for j in (i - 1, i, i + 1):
                            if 0 <= j < len(run):
                                keep[j] = True
                i = 0
                while i < len(run):
                    if keep[i]:
                        j = i
                        while j + 1 < len(run) and keep[j + 1]:
                            j += 1
                        if j > i:
                            spans.append((run[i][0], run[j][1]))
                        i = j + 1
                    else:
                        i += 1
        run.clear()

    prev_end = -1
    for m in _NAME_TOKEN.finditer(text):
        tok = m.group()
        if run:
            sep = text[prev_end : m.start()]
            if len(sep) > 3 or not (sep.isspace() or (sep[:1] == "," and sep[1:].isspace())):
                flush()
        if _is_name_token(tok):
            run.append((m.start(), m.end(), _is_name_hit(tok)))
        else:
            flush()
        prev_end = m.end()
    flush()
    return spans


class PersonNameDetector:
    """A whole value that is a person name: 2-4 name tokens, at least one in the bundled list.

    Accepts a title prefix (``Dr. Maria Garcia``), ``"Garcia, Maria"``, ALL-CAPS, ``McX``,
    ``O'X`` and hyphenated forms. Set ``enabled=False`` (or re-register a disabled instance)
    to opt out; ``mask_value`` then leaves names alone too.
    """

    name = "person_name"
    category = "name"

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled

    def matches(self, value: str) -> bool:
        if not self.enabled:
            return False
        v = value.strip()
        t = _TITLE.match(v)
        if t:
            v = v[t.end() :]
        spans = name_spans(v)
        return len(spans) == 1 and spans[0] == (0, len(v)) and len(v.split()) <= 4


def names_enabled() -> bool:
    """False when the registered ``person_name`` detector is disabled or replaced by a stub."""
    for d in _registry:
        if d.name == "person_name":
            return bool(getattr(d, "enabled", True))
    return False


_BUILTINS: tuple[Detector, ...] = (
    _Fn("uuid", "uuid", lambda v: UUID_RE.fullmatch(v) is not None),
    _Fn("email", "email", _email),
    _Fn("ipv4", "ip", ipv4_ok),
    _Fn("ipv6", "ip", ipv6_ok),
    _Fn("iban", "iban", _iban),
    _Fn("card", "card", _card),
    _Fn("hex", "hex", hex_ok),
    _Fn("phone", "phone", _phone),
    _Fn("secret", "secret", _secret),
    _Fn("address", "address", _address),
    PersonNameDetector(),
    _Fn("free_text", "free_text", _free_text),
)

_registry: list[Detector] = list(_BUILTINS)


def all_detectors() -> tuple[Detector, ...]:
    """Registered detectors in priority order (built-ins first, then plug-ins)."""
    return tuple(_registry)


def register_detector(d: Detector, *, replace: bool = False) -> None:
    """Add a plug-in detector. A duplicate ``name`` raises unless ``replace=True``."""
    for i, existing in enumerate(_registry):
        if existing.name == d.name:
            if not replace:
                raise ValueError(f"detector {d.name!r} is already registered")
            _registry[i] = d
            return
    _registry.append(d)


def reset_detectors() -> None:
    """Restore the built-in set (for tests)."""
    _registry[:] = list(_BUILTINS)


def first_match(value: str) -> Detector | None:
    v = value.strip()
    if not v:
        return None
    for d in _registry:
        if d.matches(v):
            return d
    return None


# ---------------------------------------------------------------------------------------------
# Column classification: shape first, name second, cardinality third.

_NAME_HINTS: tuple[tuple[str, str], ...] = (
    ("email", "email"),
    ("e_mail", "email"),
    ("phone", "phone"),
    ("mobile", "phone"),
    ("iban", "iban"),
    ("card", "card"),
    ("ip_addr", "ip"),
    ("ipaddr", "ip"),
    ("address", "address"),
    ("street", "address"),
    ("first_name", "name"),
    ("last_name", "name"),
    ("full_name", "name"),
    ("surname", "name"),
    ("token", "secret"),
    ("secret", "secret"),
    ("password", "secret"),
)

SHAPE_THRESHOLD = 0.5
HINTED_THRESHOLD = 0.2
LOW_CARDINALITY_MAX = 20
_TRAP_CATEGORIES = frozenset({"email", "hex", "uuid", "secret"})


def _name_hint(name: str) -> str | None:
    norm = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower()
    for needle, category in _NAME_HINTS:
        if needle in norm:
            return category
    return None


def classify_column(
    values: Sequence[tuple[str, int]], distinct: int | None, name: str
) -> PIIFinding | None:
    """Classify a column from its observed ``(value, count)`` pairs.

    1. Shape: the detector matching the largest share of non-blank observations wins if it
       covers at least half of them (``SHAPE_THRESHOLD``), or 20% (``HINTED_THRESHOLD``) when
       the column name points at the same category.
    2. Name: a name hint only lowers that threshold; a name alone never classifies.
    3. Cardinality: a column with at most 20 distinct values whose values look like
       hashes/emails/UUIDs/secrets gets ``category="review"`` (an anonymised enum looks like
       identity by shape and like an enum by cardinality). Unknown cardinality (``None``)
       skips the trap.
    """
    total = 0
    by_det: Counter[str] = Counter()
    det_obj: dict[str, Detector] = {}
    for value, count in values:
        if not isinstance(value, str) or not value.strip() or count <= 0:
            continue
        total += count
        d = first_match(value)
        if d is not None:
            by_det[d.name] += count
            det_obj[d.name] = d
    if total == 0 or not by_det:
        return None
    dname, hits = by_det.most_common(1)[0]
    det = det_obj[dname]
    share = hits / total
    hint = _name_hint(name)
    threshold = HINTED_THRESHOLD if hint == det.category else SHAPE_THRESHOLD
    if share < threshold:
        return None
    pct = f"{share:.0%} of observed values match the {det.name} shape"
    if hint == det.category:
        pct += f" and the column name suggests {hint}"
    if (
        det.category in _TRAP_CATEGORIES
        and distinct is not None
        and distinct <= LOW_CARDINALITY_MAX
    ):
        return PIIFinding(
            "review",
            det.name,
            f"{pct}, but only {distinct} distinct values: an anonymised enum looks like this; "
            "decide by hand",
        )
    return PIIFinding(det.category, det.name, pct)
