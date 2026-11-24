"""``docs/reference/diagnostics.md`` == ``CODES`` == the codes the source emits (spec 20)."""

from __future__ import annotations

import re
from pathlib import Path

from schemaguru.diagnostics import CODES

ROOT = Path(__file__).resolve().parents[2]
CODE_RE = re.compile(r"\bSG\d{3}\b")


def doc_codes(text: str) -> dict[str, str]:
    """``{code: meaning}`` from the reference table's rows (``| SGnnn | meaning |``)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r"^\|\s*(SG\d{3})\s*\|\s*(.*?)\s*\|\s*$", line)
        if m:
            out[m.group(1)] = m.group(2).replace("\\|", "|")
    return out


def source_codes() -> set[str]:
    codes: set[str] = set()
    for path in (ROOT / "src" / "schemaguru").rglob("*.py"):
        codes |= set(CODE_RE.findall(path.read_text(encoding="utf-8")))
    return codes


def test_doc_table_equals_codes() -> None:
    doc = doc_codes((ROOT / "docs" / "reference" / "diagnostics.md").read_text(encoding="utf-8"))
    assert doc == dict(CODES)


def test_source_emits_exactly_the_registered_codes() -> None:
    assert source_codes() == set(CODES)


def test_negative_controls() -> None:
    text = "| Code | Meaning |\n|---|---|\n| SG101 | a \\| b |\n| SG999 | planted |\n"
    parsed = doc_codes(text)
    assert parsed == {"SG101": "a | b", "SG999": "planted"}
    assert parsed != dict(CODES)  # a planted or missing row is seen
    assert "SG999" not in source_codes()
