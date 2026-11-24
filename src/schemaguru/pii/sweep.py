"""Outbound shape sweep (spec 11): mask any detector hit inside a free string.

Unlike whole-value detection this *searches*, so it only uses detectors whose shape is
unambiguous inside prose: JWT, email, UUID, IBAN (checksum), card (Luhn), IPv4/IPv6, long
hex, E.164 / formatted national phone numbers. Names, addresses, free text and entropy
secrets are whole-value heuristics and are not applied here.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from schemaguru.pii import detectors as d

_Rule = tuple[str, "re.Pattern[str]", Callable[[str], bool], Callable[[str], str]]


def _b(pattern: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w.+@\-]){pattern}(?![\w@\-])")


def _tok(token: str) -> Callable[[str], str]:
    return lambda _m: token


_KV = re.compile(
    r"\b(?P<key>api[_-]?key|access[_-]?key|key|token|secret|password|passwd|bearer|authorization)"
    r"(?P<sep>[ \t]{0,3}[:=][ \t]{0,3}|[ \t]{1,3})(?P<val>\S{8,})",
    re.I,
)
_PROVIDER_KEY = re.compile(r"\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{8,}")


_KNOWN_PREFIX = re.compile(
    r"(?:sk|pk|rk)_(?:live|test)_|gh[pousr]_|github_pat_|AKIA[0-9A-Z]|xox[abprs]-|xapp-|eyJ"
)


# Bare credentials in prose, by their published shapes (fixed or bounded lengths: linear time).
_CREDENTIAL_SHAPES = (
    r"(?:AKIA|ASIA)[0-9A-Z]{16}",  # AWS access key id
    r"gh[pousr]_[A-Za-z0-9]{36,255}",  # GitHub tokens
    r"github_pat_[A-Za-z0-9_]{40,255}",  # GitHub fine-grained tokens
    r"xox[abposr]-[A-Za-z0-9\-]{10,255}",  # Slack tokens
    r"xapp-[A-Za-z0-9\-]{20,255}",  # Slack app-level tokens
    r"AIza[0-9A-Za-z_\-]{35}",  # Google API key
)


def _token_like(value: str) -> bool:
    """A value that looks like a credential, not an identifier or a word.

    Known provider prefix, or 16+ chars mixing letters and digits, or a high-entropy secret
    (the whole-value secret detector). ``customer_id``, ``required_header`` and ``SKU-1234``
    are not.
    """
    v = value.strip("\"'`.,;)]}")
    if _KNOWN_PREFIX.match(v):
        return True
    if len(v) >= 16 and any(c.isdigit() for c in v) and any(c.isalpha() for c in v):
        return True
    return d.secret_like(v)


def _kv_ok(text: str) -> bool:
    m = _KV.fullmatch(text)
    if m is None or m["val"].startswith("<"):
        return False
    v = m["val"].strip("\"'`.,;)]}")
    if m["key"].lower() in ("password", "passwd") and any(c.isdigit() for c in v):
        return any(c.isalpha() for c in v)  # a password with digits and letters, any length
    return _token_like(m["val"])


def _kv_sub(text: str) -> str:
    m = _KV.fullmatch(text)
    assert m is not None
    return m["key"] + m["sep"] + "<secret>"


def _iban_prefix(text: str) -> int:
    """Length of the longest checksum-valid IBAN prefix of ``text`` (0 if none).

    The search regex is greedy and may swallow following words (``"... 432 today"``), so the
    candidate is trimmed back to a prefix that validates and ends at a word boundary.
    """
    for end in range(len(text), 14, -1):
        if (end == len(text) or not text[end].isalnum()) and d.iban_ok(text[:end].rstrip()):
            return end
    return 0


def _iban_ok(text: str) -> bool:
    return _iban_prefix(text) > 0


def _iban_sub(text: str) -> str:
    end = _iban_prefix(text)
    return "<iban>" + text[len(text[:end].rstrip()) :]


_RULES: tuple[_Rule, ...] = (
    ("secret", _b(d.JWT_RE.pattern), d.jwt_ok, _tok("<secret>")),
    ("secret", _KV, _kv_ok, _kv_sub),
    ("secret", _PROVIDER_KEY, lambda _m: True, _tok("<secret>")),
    *[("secret", _b(pat), lambda _m: True, _tok("<secret>")) for pat in _CREDENTIAL_SHAPES],
    ("email", re.compile(d.EMAIL_RE.pattern), lambda _m: True, _tok("<email>")),
    ("uuid", _b(d.UUID_RE.pattern), lambda _m: True, _tok("<uuid>")),
    ("iban", _b(d.IBAN_RE.pattern), _iban_ok, _iban_sub),
    ("card", _b(d.CARD_RE.pattern), d.card_ok, _tok("<card>")),
    ("ip", re.compile(rf"(?<![\w.]){d.IPV4_RE.pattern}(?![\w]|\.\d)"), d.ipv4_ok, _tok("<ip>")),
    ("ip", re.compile(rf"(?<![\w:]){d.IPV6_RE.pattern}(?![\w:])"), d.ipv6_ok, _tok("<ip>")),
    ("hex", _b(d.HEX_RE.pattern), d.hex_ok, lambda m: f"<hex{len(m)}>"),
    ("phone", re.compile(rf"(?<![\w+]){d.E164_RE.pattern}(?!\d)"), d.phone_ok, _tok("<phone>")),
    (
        "phone",
        re.compile(rf"(?<![\w.]){d.NATIONAL_RE.pattern}(?![\w])"),
        d.phone_ok,
        _tok("<phone>"),
    ),
)


def sweep_detail(payload: str) -> tuple[str, list[str]]:
    """Return the masked text and the category of every hit, in rule order."""
    hits: list[str] = []
    text = payload
    for category, rx, ok, token in _RULES:

        def repl(
            m: re.Match[str],
            category: str = category,
            ok: Callable[[str], bool] = ok,
            token: Callable[[str], str] = token,
        ) -> str:
            if not ok(m.group(0)):
                return m.group(0)
            hits.append(category)
            return token(m.group(0))

        text = rx.sub(repl, text)
    return text, hits


def sweep(payload: str) -> tuple[str, int]:
    """Mask every detector hit inside ``payload``; return ``(masked_text, hit_count)``.

    Idempotent: a masked token matches no rule, so sweeping twice reports zero hits.
    """
    text, hits = sweep_detail(payload)
    return text, len(hits)
