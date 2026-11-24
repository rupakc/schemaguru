"""Output formats of a :class:`~schemaguru.render.view.ContextView` (spec 13.1, 21).

``text`` (the default), ``markdown``, ``json`` (canonical: sorted keys, compact, byte-
deterministic) and ``xml`` (tagged sections for models that prefer them). The set is open:
``register_format(name, fn)``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import TYPE_CHECKING
from xml.sax.saxutils import escape, quoteattr

from schemaguru._util.hashing import canonical_json

if TYPE_CHECKING:
    from schemaguru.render.view import ContextView, ViewBlock

# Characters XML 1.0 cannot carry (warehouse text may hold them): replaced, never dropped.
_XML_BAD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


def _title(b: ViewBlock) -> str:
    return b.title or b.kind.replace("_", " ").capitalize()


def to_markdown(view: ContextView) -> str:
    question = " ".join(view.question.split())
    parts = [f"# Context: {question}"]
    for b in view.blocks:
        runs = [len(m) for m in re.findall(r"`+", b.text)]
        fence = "`" * max(3, max(runs, default=0) + 1)
        parts.append(f"## {_title(b)}\n\n{fence}text\n{b.text}\n{fence}")
    return "\n\n".join(parts) + "\n"


def to_json(view: ContextView) -> str:
    return canonical_json(
        {
            "question": view.question,
            "fingerprint": view.fingerprint,
            "tokens": view.tokens,
            "tables": [t.canonical() for t in view.tables],
            "blocks": [
                {"kind": b.kind, "title": _title(b), "stable": b.stable, "text": b.text}
                for b in view.blocks
            ],
            "diagnostics": [d.to_dict() for d in view.diagnostics],
        }
    )


def _x(text: str) -> str:
    return _XML_BAD.sub("�", text)


def to_xml(view: ContextView) -> str:
    q = quoteattr(_x(" ".join(view.question.split())))
    lines = [f'<context question={q} fingerprint="{view.fingerprint}" tokens="{view.tokens}">']
    for b in view.blocks:
        stable = "true" if b.stable else "false"
        lines.append(
            f"<block kind={quoteattr(b.kind)} name={quoteattr(_x(_title(b)))} "
            f'stable="{stable}">{escape(_x(b.text))}</block>'
        )
    lines.append("</context>")
    return "\n".join(lines)


Formatter = Callable[["ContextView"], str]

_FORMATS: dict[str, Formatter] = {
    "text": lambda v: v.text,
    "markdown": to_markdown,
    "json": to_json,
    "xml": to_xml,
}


def register_format(name: str, fn: Formatter, *, replace: bool = False) -> None:
    if name in _FORMATS and not replace:
        raise ValueError(f"format already registered: {name}")
    _FORMATS[name] = fn


def format_view(view: ContextView, name: str) -> str:
    try:
        fn = _FORMATS[name]
    except KeyError:
        raise KeyError(f"unknown format: {name} (known: {', '.join(sorted(_FORMATS))})") from None
    return fn(view)
