"""Value masking (spec 11): replace personal values with their shape token."""

from __future__ import annotations

from schemaguru.pii.detectors import first_match, name_spans, names_enabled
from schemaguru.pii.sweep import sweep_detail

_TOKENS = {
    "email": "<email>",
    "phone": "<phone>",
    "name": "<name>",
    "iban": "<iban>",
    "card": "<card>",
    "ip": "<ip>",
    "uuid": "<uuid>",
    "secret": "<secret>",
    "address": "<address>",
}


def mask_value(value: object) -> tuple[str, str | None]:
    """Return ``(masked_text, category)``; ``category`` is None when nothing was masked.

    ``None`` and blank strings are NULL for detection: ``("", None)``. A value whose whole
    text has a PII shape becomes that shape's token (``<hex{n}>`` for hex of n chars,
    ``<free_text len=N>`` for long prose). Otherwise any embedded hit (an email inside a short
    sentence) is masked in place, so a value never leaves with an identifier intact.
    """
    if value is None:
        return "", None
    text = value if isinstance(value, str) else str(value)
    if not text.strip():
        return "", None
    det = first_match(text)
    if det is not None:
        cat = det.category
        if cat == "free_text":
            return f"<free_text len={len(text.strip())}>", cat
        if cat == "hex":
            return f"<hex{len(text.strip())}>", cat
        return _TOKENS.get(cat, f"<{cat}>"), cat
    masked, hits = sweep_detail(text)
    if names_enabled():
        spans = name_spans(masked)
        if spans:
            out: list[str] = []
            pos = 0
            for a, b in spans:
                out.append(masked[pos:a])
                out.append("<name>")
                pos = b
            out.append(masked[pos:])
            masked = "".join(out)
            hits.append("name")
    if hits:
        return masked, hits[0]
    return text, None
