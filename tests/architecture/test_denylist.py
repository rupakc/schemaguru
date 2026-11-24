"""No proprietary names anywhere (spec 22, 25.20).

The denylist is never committed: CI passes it through the secret ``SCHEMAGURU_DENYLIST``
(newline-separated, case-insensitive). The repo scan skips when it is unset.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent


def parse_denylist(raw: str) -> list[str]:
    return [line.strip().lower() for line in raw.splitlines() if line.strip()]


def term_pattern(entry: str) -> re.Pattern[str]:
    """Every case-insensitive occurrence of one denylist term (overlapping), edges unchecked.

    Whitespace inside a term matches any run of whitespace, ``_`` or ``-``. A term is text, not
    a regex. :func:`contains_term` decides which occurrences are whole words.
    """
    words = [re.escape(w) for w in entry.split()]
    body = r"[\s_-]+".join(words)
    return re.compile(rf"(?=({body}))", re.IGNORECASE)


def _edge(text: str, i: int) -> bool:
    """Is there a word edge between ``text[i - 1]`` and ``text[i]``?

    Yes at either end of the text, next to anything not a letter or digit (``_``, ``-``, ``.``,
    ``/`` and spaces separate words), at a lower -> upper case change (``AcmeClient``), where an
    upper-case run meets a capitalised word (``ACMEClient``: before the ``C``) and between a
    letter and a digit (``acme2``)."""
    if i <= 0 or i >= len(text):
        return True
    a, b = text[i - 1], text[i]
    if not (a.isalnum() and b.isalnum()):
        return True
    if a.islower() and b.isupper():
        return True
    if a.isupper() and b.isupper() and i + 1 < len(text) and text[i + 1].islower():
        return True
    return a.isalpha() != b.isalpha()


def contains_term(entry: str, text: str) -> bool:
    """Whole-word, case-insensitive: ``zorb quax`` matches ``Zorb  Quax``, ``zorb_quax`` and
    ``MyZorb Quax2``; never the ``zorb quax`` inside ``metazorb quax``."""
    for m in term_pattern(entry).finditer(text):
        start, end = m.start(1), m.end(1)
        if _edge(text, start) and _edge(text, end):
            return True
    return False


def find_violations(files: list[Path], entries: list[str], root: Path) -> list[str]:
    """Whole-word, case-insensitive scan of text files; binary files are skipped."""
    found: list[str] = []
    terms = [e for e in entries if e.strip()]
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        rel = path.relative_to(root).as_posix()
        for term in terms:
            if contains_term(term, text):
                found.append(f"{rel}: contains a denylisted term")
            if contains_term(term, rel):
                found.append(f"{rel}: path contains a denylisted term")
    return found


def tracked_files(root: Path = ROOT) -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True, text=True
    ).stdout
    return [root / name for name in out.split("\0") if name]


def test_parse_denylist() -> None:
    assert parse_denylist(" Foo \n\n bar\r\n") == ["foo", "bar"]
    assert parse_denylist("") == []


def test_negative_control_scan_flags_text_and_paths(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("we love Acme Widgets\n", encoding="utf-8")
    (tmp_path / "acme_notes.txt").write_text("clean\n", encoding="utf-8")
    (tmp_path / "clean.txt").write_text("nothing here\n", encoding="utf-8")
    (tmp_path / "blob.bin").write_bytes(b"\xff\xfe\x00acme")
    files = sorted(tmp_path.iterdir())
    hits = find_violations(files, ["acme"], tmp_path)
    assert any(h.startswith("a.md") for h in hits)
    assert any(h.startswith("acme_notes.txt") and "path" in h for h in hits)
    assert not any(h.startswith(("clean.txt", "blob.bin")) for h in hits)
    assert find_violations(files, [], tmp_path) == []


def test_terms_match_whole_words_only(tmp_path: Path) -> None:
    """Negative control for the substring false positive: a two-word term never matches inside
    a longer word, and still matches across case and whitespace."""
    term = "zorb quax"  # invented: a test file must never hold a real denylisted term
    clean = tmp_path / "clean.md"
    clean.write_text(
        "the metazorb quax refuses writes; metazorbquax and zorb quaxes too\n", encoding="utf-8"
    )
    assert find_violations([clean], [term], tmp_path) == []
    for i, text in enumerate(("Zorb Quax", "ZORB  QUAX", "a zorb\nquax", "(zorb quax)")):
        hit = tmp_path / f"hit{i}.md"
        hit.write_text(f"x {text} y\n", encoding="utf-8")
        assert find_violations([hit], [term], tmp_path) == [
            f"hit{i}.md: contains a denylisted term"
        ]
    named = tmp_path / "zorb_quax_notes.txt"
    named.write_text("clean\n", encoding="utf-8")
    assert find_violations([named], [term], tmp_path) == [
        "zorb_quax_notes.txt: path contains a denylisted term"
    ]
    assert not contains_term("a.b", "axb")  # a term is text, not a regex


def test_camel_case_and_digit_edges() -> None:
    """One-word terms inside identifiers: a lower -> upper change and a letter <-> digit change
    are word edges (``ZorbClient``, ``zorb2``); a plain lower-case run is not (negative control)."""
    term = "zorb"  # invented
    for hit in (
        "ZorbClient",
        "new ZorbClient()",
        "MyZorbClient",
        "zorb2",
        "v2zorb",
        "ZORB_API",
        "zorbHub",
        "ZORBClient",  # an all-caps acronym before a capitalised word
        "new ZORBClient()",
        "MyZORBClient",
    ):
        assert contains_term(term, hit), hit
    for clean in ("zorbclient", "Metazorb", "zorbs", "ZORBA", "subzorbed", "Azorb"):
        assert not contains_term(term, clean), clean


@pytest.mark.skipif(
    not os.environ.get("SCHEMAGURU_DENYLIST"), reason="SCHEMAGURU_DENYLIST is not set"
)
def test_repo_has_no_denylisted_terms() -> None:
    if not (ROOT / ".git").exists():  # an unpacked sdist: no git, nothing tracked to scan
        pytest.skip("not a git checkout (sdist)")
    entries = parse_denylist(os.environ["SCHEMAGURU_DENYLIST"])
    assert find_violations(tracked_files(), entries, ROOT) == []
