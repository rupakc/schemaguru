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


_RULES: tuple[_Rule, ...] = (
    ("secret", _b(d.JWT_RE.pattern), d.jwt_ok, _tok("<secret>")),
    ("email", re.compile(d.EMAIL_RE.pattern), lambda _m: True, _tok("<email>")),
    ("uuid", _b(d.UUID_RE.pattern), lambda _m: True, _tok("<uuid>")),
    ("iban", _b(d.IBAN_RE.pattern), d.iban_ok, _tok("<iban>")),
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
