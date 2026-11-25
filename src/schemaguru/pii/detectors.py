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
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Protocol, runtime_checkable

from schemaguru import plugins

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

# Addresses *inside* prose (the outbound sweep searches with these). Whole-value detection above
# can afford any-case words; in a sentence the name words must be capitalised ("42 Elm Street"),
# except before an unambiguous street word ("12 baker street"), so prose such as "updated
# 2 times via the API" or "rank 1 takes first place" stays. Every repetition is bounded and each
# pattern starts only at a word start: linear time.
_CAP = "[A-Z\u00c0-\u00d6\u00d8-\u00de][\\w'\\-]{0,30}"
_TEXT_WORD = r"[^\W\d_][\w'\-]{0,30}"
_STRONG_STREET = r"street|road|avenue|boulevard|blvd|lane|stra(?:\u00df|ss)e"
_CITY = rf"(?:,[ \t]{{0,2}}(?:\d{{4,5}}[ \t]{{1,3}})?{_CAP})?"
ADDRESS_IN_TEXT_RES = (
    # "42 Elm Street, Springfield": house number, 1-4 capitalised words, a street word, a city
    re.compile(
        rf"(?<![\w.,])\d{{1,4}}[A-Za-z]?[ \t]{{1,3}}(?:{_CAP}[ \t]{{1,3}}){{1,4}}"
        rf"(?i:{_STREET})\b\.?{_CITY}"
    ),
    # "12 baker street": any-case words before an unambiguous street word
    re.compile(
        rf"(?<![\w.,])\d{{1,4}}[A-Za-z]?[ \t]{{1,3}}(?:{_TEXT_WORD}[ \t]{{1,3}}){{1,3}}"
        rf"(?i:{_STRONG_STREET})\b{_CITY}"
    ),
    # "Baker Street 12"
    re.compile(
        rf"(?<![\w])(?:{_CAP}[ \t]{{1,3}}){{1,4}}(?i:{_STREET})\b\.?[ \t]{{0,3}}"
        rf"\d{{1,4}}[A-Za-z]?(?!\w){_CITY}"
    ),
    # glued German forms: "Hauptstra\u00dfe 12", "Musterstr. 5a"
    re.compile(
        r"(?<![\w])(?i:[^\W\d_]{3,40}(?:stra(?:\u00df|ss)e|weg|platz|allee|gasse)"
        r"|[^\W\d_]{4,40}str\.)[ \t]{0,3}\d{1,4}[A-Za-z]?(?!\w)" + _CITY
    ),
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

    def __init__(self, name: str, category: str, fn: Callable[[str], bool]) -> None:
        self.name = name
        self.category = category
        self._fn = fn

    def matches(self, value: str) -> bool:
        return bool(self._fn(value.strip()))


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


# A path or URL ("/var/data/x.csv", "./out/v3.csv", "s3://bucket/key"; no whitespace, so prose
# that starts with '/' is still free text): its own shape is not a secret, so it is one only when
# a piece of it looks random. A '/'-segment that is wholly hex or
# a UUID is left to the outbound sweep, which masks it in place ("/var/data/<hex32>/x.csv").
_PATH_START = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://|\.{1,2}/|~/|/")
_PIECE = re.compile(r"[A-Za-z0-9]+")
_SWEPT_SEGMENT = re.compile(rf"(?:{UUID_RE.pattern}|[0-9a-fA-F]{{20,}})(?:\.[A-Za-z0-9]{{1,8}})?")


_CAMEL = re.compile(r"(?:[A-Z]?[a-z]+)+|[A-Z]+[a-z]*")


def _random_piece(p: str) -> bool:
    """An alphanumeric run that could be (part of) a token: 8+ characters mixing letters and
    digits or mixing case; 5-7 characters mixing case with a digit (``3OYmpN``) or in no
    camel-case pattern (``PoiCtT``); or 16+ letters of high entropy. Words, numbers, dates and
    ``iPhone`` / ``MyApp`` are not."""
    if len(p) < 5:
        return False
    if len(p) < 8:
        mixed = any(c.isupper() for c in p) and any(c.islower() for c in p) and not p.istitle()
        return mixed and (not p.isalpha() or _CAMEL.fullmatch(p) is None)
    letters = any(c.isalpha() for c in p)
    if letters and any(c.isdigit() for c in p):
        return True
    if not letters:
        return False
    if not (p.islower() or p.isupper() or p.istitle()):
        return True
    return len(p) >= 16 and shannon(p) >= 3.5


def _path_secret(v: str) -> bool:
    for segment in v.split("/"):
        head = segment.split(".")[0]
        if _SWEPT_SEGMENT.fullmatch(segment) and (UUID_RE.fullmatch(head) or hex_ok(head)):
            continue  # the sweep masks it in place
        if any(_random_piece(p) for p in _PIECE.findall(segment)):
            return True
    return False


def _entropy_secret(v: str) -> bool:
    if len(v) < 24 or not _SECRET_CHARS.fullmatch(v) or _SCHEME_RE.match(v):
        return False
    if UUID_RE.fullmatch(v) or hex_ok(v):
        return False
    if not (any(c.isdigit() for c in v) and any(c.isalpha() for c in v)):
        return False
    return shannon(v) >= 4.0


def _secret(v: str) -> bool:
    if jwt_ok(v) and JWT_RE.fullmatch(v):
        return True
    if _PATH_START.match(v) and not any(c.isspace() for c in v):
        # '+' is base64, almost never a path: such a value keeps the entropy rule too
        return _path_secret(v) or ("+" in v and _entropy_secret(v))
    return _entropy_secret(v)


def secret_like(value: str) -> bool:
    """Whole-value secret shape (JWT or high-entropy token); see the ``secret`` detector."""
    return _secret(value.strip())


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


_HONORIFICS = frozenset(
    {"mr", "mrs", "ms", "miss", "dr", "prof", "herr", "frau", "mme", "mlle", "sir"}
)
_CUE_WORDS = frozenset({"customer", "contact", "by", "from"})
_CUE_TAIL = re.compile(r"([^\W\d_]{2,8})\.?\s{1,3}\Z")


@dataclass(frozen=True)
class _Tok:
    start: int
    end: int
    word: str
    hit: bool  # in the name list
    cap: bool  # a capitalised name token (False: a lower-case token that is in the list)
    possessive: bool


def _cue_before(text: str, start: int) -> str | None:
    """``"honorific"`` / ``"cue"`` when the word just before ``start`` announces a person."""
    m = _CUE_TAIL.search(text, max(0, start - 13), start)
    if m is None:
        return None
    a = m.start(1)
    if a > 0 and (text[a - 1].isalnum() or text[a - 1] == "_"):
        return None  # the tail of a longer word ("supercustomer")
    word = m.group(1).lower()
    if word in _HONORIFICS:
        return "honorific"
    return "cue" if word in _CUE_WORDS else None


def _capitalised_ranges(text: str, run: list[_Tok], i: int, j: int) -> list[tuple[int, int]]:
    """Token ranges to mask in the capitalised sub-run ``run[i..j]``."""
    hits = [k for k in range(i, j + 1) if run[k].hit]
    n = j - i + 1
    if hits and n >= 2:
        if n <= 4:
            return [(i, j)]
        keep = {m for k in hits for m in (k - 1, k, k + 1) if i <= m <= j}
        out: list[tuple[int, int]] = []
        k = i
        while k <= j:
            if k in keep:
                e = k
                while e + 1 <= j and e + 1 in keep:
                    e += 1
                if e > k:
                    out.append((k, e))
                k = e + 1
            else:
                k += 1
        return out
    if hits and run[i].possessive:
        return [(i, i)]  # a lone listed surname with a possessive: "Garcia's order"
    cue = _cue_before(text, run[i].start)
    if cue == "honorific" or (cue == "cue" and n >= 2):
        return [(i, min(j, i + 3))]
    for k in range(i, j):
        word = run[k].word.lower()
        if word in _HONORIFICS or (word in _CUE_WORDS and j - k >= 2):
            return [(k + 1, min(j, k + 4))]
    return []


def _run_spans(text: str, run: list[_Tok]) -> list[tuple[int, int]]:
    picked: list[tuple[int, int]] = []
    for want_cap in (True, False):
        i = 0
        while i < len(run):
            ok = run[i].cap if want_cap else run[i].hit
            if not ok:
                i += 1
                continue
            j = i
            while j + 1 < len(run) and (run[j + 1].cap if want_cap else run[j + 1].hit):
                j += 1
            if want_cap:
                picked += _capitalised_ranges(text, run, i, j)
            elif j > i and not all(run[k].cap for k in range(i, j + 1)):
                picked.append((i, j))  # listed names in lower case: "maria garcia"
            i = j + 1
    merged: list[tuple[int, int]] = []
    for a, b in sorted(picked):
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return [(run[a].start, run[b].end) for a, b in merged]


def _joins(sep: str, after_initial: bool) -> bool:
    """Whether the text between two tokens keeps them in one name."""
    if len(sep) > 3:
        return False
    if sep.isspace():
        return True
    if sep[:1] == "," or (sep[:1] == "." and after_initial):
        return sep[1:] == "" or sep[1:].isspace()
    return False


def name_spans(text: str) -> list[tuple[int, int]]:
    """Spans of person names inside ``text``.

    Tokens are Title-case, ALL-CAPS, ``McX``/``MacX``, ``O'X`` or hyphenated (capitalised), or
    a lower-case word that is in the bundled name list. They are joined by whitespace, by a
    comma with or without a space (``"Garcia, Maria"``, ``"Garcia,Maria"``) or by an initial's
    period (``"M. Garcia"``). A name is:

    * a run of 2-4 capitalised tokens with at least one hit in the name list (a longer run is
      masked in windows of one token either side of each hit);
    * 2+ consecutive listed tokens in any case (``"maria garcia"``);
    * a lone listed token with a possessive (``"Garcia's"``); a trailing ``'s`` / ``U+2019 s``
      ends the name and is kept outside the span (``"<name>'s"``);
    * 1-4 capitalised tokens after an honorific (``Mr``, ``Ms``, ``Dr``, ...), or 2-4 after a
      person cue (``customer``, ``contact``, ``by``, ``from``), listed or not.

    Linear in len(text).
    """
    spans: list[tuple[int, int]] = []
    run: list[_Tok] = []

    def flush() -> None:
        if run:
            spans.extend(_run_spans(text, run))
        run.clear()

    prev_end = -1
    after_initial = False
    for m in _NAME_TOKEN.finditer(text):
        tok = m.group()
        if run and not _joins(text[prev_end : m.start()], after_initial):
            flush()
        end, possessive = m.end(), False
        if len(tok) > 2 and tok[-2] in "'\u2019" and tok[-1] in "sS":
            base = tok[:-2]  # "Johnson's": mask the name, keep the possessive suffix
            if _is_name_token(base):
                tok, end, possessive = base, m.start() + len(base), True
        if _is_name_token(tok):
            run.append(_Tok(m.start(), end, tok, _is_name_hit(tok), True, possessive))
            if possessive:
                flush()  # a possessive ends the name
        elif tok.islower() and _is_name_hit(tok):
            run.append(_Tok(m.start(), end, tok, True, False, False))
        else:
            flush()
        after_initial = len(tok) == 1 and tok.isupper()
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
    _discover()
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


def _plugin(name: str, obj: object) -> None:
    d = plugins.instance(obj)
    if not (isinstance(getattr(d, "name", None), str) and callable(getattr(d, "matches", None))):
        raise TypeError("a PII detector plugin needs a str name and matches(value)")
    register_detector(d)


def _discover() -> None:
    """Entry points ``schemaguru.pii_detectors`` (spec 21), once, on first use."""
    plugins.discover("pii_detectors", _plugin)


def all_detectors() -> tuple[Detector, ...]:
    """Registered detectors in priority order (built-ins first, then plug-ins)."""
    _discover()
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
    _discover()
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
