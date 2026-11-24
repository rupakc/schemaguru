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
    """A whole-word, case-insensitive pattern for one denylist term.

    Word edges are letters and digits only (``_``, ``-``, ``.``, ``/`` separate words, so
    ``acme_notes`` still names ``acme``); whitespace inside a term matches any run of whitespace,
    ``_`` or ``-``. So ``data guard`` matches ``Data  Guard`` and ``data_guard`` but never the
    ``data guard`` inside ``metadata guard``.
    """
    words = [re.escape(w) for w in entry.split()]
    body = r"[\s_-]+".join(words)
    return re.compile(rf"(?<![^\W_]){body}(?![^\W_])", re.IGNORECASE)


def find_violations(files: list[Path], entries: list[str], root: Path) -> list[str]:
    """Whole-word, case-insensitive scan of text files; binary files are skipped."""
    found: list[str] = []
    patterns = [term_pattern(e) for e in entries if e.strip()]
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        rel = path.relative_to(root).as_posix()
        for pattern in patterns:
            if pattern.search(text):
                found.append(f"{rel}: contains a denylisted term")
            if pattern.search(rel):
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
    term = "data guard"
    clean = tmp_path / "clean.md"
    clean.write_text("the metadata guard refuses writes; metadataguard too\n", encoding="utf-8")
    assert find_violations([clean], [term], tmp_path) == []
    for i, text in enumerate(("Data Guard", "DATA  GUARD", "a data\nguard", "(data guard)")):
        hit = tmp_path / f"hit{i}.md"
        hit.write_text(f"x {text} y\n", encoding="utf-8")
        assert find_violations([hit], [term], tmp_path) == [
            f"hit{i}.md: contains a denylisted term"
        ]
    named = tmp_path / "data_guard_notes.txt"
    named.write_text("clean\n", encoding="utf-8")
    assert find_violations([named], [term], tmp_path) == [
        "data_guard_notes.txt: path contains a denylisted term"
    ]
    assert term_pattern("a.b").search("axb") is None  # a term is text, not a regex


@pytest.mark.skipif(
    not os.environ.get("SCHEMAGURU_DENYLIST"), reason="SCHEMAGURU_DENYLIST is not set"
)
def test_repo_has_no_denylisted_terms() -> None:
    entries = parse_denylist(os.environ["SCHEMAGURU_DENYLIST"])
    assert find_violations(tracked_files(), entries, ROOT) == []
