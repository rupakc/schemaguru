"""The 0.2.0 hardening ledger (spec H1, Appendix A) lists every deferred 0.1.0 item exactly once,
in one of four states, with evidence a reader can check:

* ``open`` names the task that owns it (``Task 3``);
* ``already fixed`` names the fixing commit and a test that pins it
  (``commit <sha> · test <path>::<name>``);
* ``fixed`` names the red-first test (``test <path>::<name>``);
* ``ruled out`` gives a one-line reason (and ``· docs: <page>`` when the item is user-visible).

A named test must exist (file present, ``def <name>`` in it) and a named commit must exist in the
repository (skipped without ``.git``, e.g. in an sdist).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
LEDGER = ROOT / "docs" / "specs" / "2026-11-25-schemaguru-0.2.0-hardening-ledger.md"

ALLOWED_STATES = ("open", "fixed", "already fixed", "ruled out")

_COUNTS = (("A1", 17), ("A2", 9), ("A3", 13), ("A4", 18), ("A5", 27), ("A6", 12), ("A7", 6))
_COUNTS += (("A8", 11), ("A9", 5))
ITEM_IDS: tuple[str, ...] = tuple(f"{area}.{i}" for area, n in _COUNTS for i in range(1, n + 1))

_ROW = re.compile(r"\|\s*(A\d\.\d+)\s*\|")
_TEST = r"test (?P<path>[\w./-]+\.py)::(?P<name>\w+)"
_EVIDENCE = {
    "open": re.compile(r"Task \d+"),
    "fixed": re.compile(_TEST),
    "already fixed": re.compile(r"commit (?P<sha>[0-9a-f]{7,40}) · " + _TEST),
    "ruled out": re.compile(r"(?P<reason>.+?)(?: · docs: (?P<docs>\S+))?"),
}


def _commit_exists(sha: str, root: Path) -> bool:
    if not (root / ".git").exists():  # an sdist: nothing to check against
        return True
    done = subprocess.run(
        ["git", "-C", str(root), "cat-file", "-e", f"{sha}^{{commit}}"],
        capture_output=True,
        check=False,
    )
    return done.returncode == 0


def evidence_problems(item_id: str, state: str, evidence: str, root: Path = ROOT) -> list[str]:
    """What is wrong with one row's state and evidence (empty when the row is valid)."""
    if state not in ALLOWED_STATES:
        return [f"{item_id}: unknown state {state!r} (allowed: {', '.join(ALLOWED_STATES)})"]
    m = _EVIDENCE[state].fullmatch(evidence)
    if m is None:
        return [f"{item_id}: {state!r} evidence {evidence!r} does not match the ledger grammar"]
    problems: list[str] = []
    if state == "ruled out" and len(m.group("reason").strip()) < 10:
        problems.append(f"{item_id}: a ruled-out reason needs at least 10 characters")
    if state in ("fixed", "already fixed"):
        path, name = m.group("path"), m.group("name")
        test_file = root / path
        if not test_file.is_file():
            problems.append(f"{item_id}: test file {path} does not exist")
        elif not re.search(rf"^\s*def {re.escape(name)}\(", test_file.read_text("utf-8"), re.M):
            problems.append(f"{item_id}: {path} defines no test {name}")
    if state == "already fixed" and not _commit_exists(m.group("sha"), root):
        problems.append(f"{item_id}: commit {m.group('sha')} is not in the repository")
    return problems


def parse_ledger(text: str, root: Path = ROOT) -> dict[str, tuple[str, str, str]]:
    """``id -> (item, state, evidence)`` for every ledger row; ``ValueError`` on a repeated id or
    any row whose state or evidence is invalid (see :func:`evidence_problems`)."""
    rows: dict[str, tuple[str, str, str]] = {}
    problems: list[str] = []
    for line in text.splitlines():
        if _ROW.match(line) is None:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != 4:
            problems.append(f"malformed row (expected 4 cells): {line!r}")
            continue
        item_id, item, state, evidence = cells
        state = state.strip("`")
        if item_id in rows:
            problems.append(f"{item_id}: listed more than once")
            continue
        rows[item_id] = (item, state, evidence)
        problems += evidence_problems(item_id, state, evidence, root)
    if problems:
        raise ValueError("\n".join(problems))
    return rows


def _ledger_text() -> str:
    return LEDGER.read_text(encoding="utf-8")


def test_ledger_lists_every_item_once() -> None:
    text = _ledger_text()
    listed = [m.group(1) for line in text.splitlines() if (m := _ROW.match(line))]
    assert len(listed) == len(set(listed)), sorted({i for i in listed if listed.count(i) > 1})
    assert set(parse_ledger(text)) == set(ITEM_IDS)
    assert listed == list(ITEM_IDS)  # in Appendix A order
    assert "| ID | Item | State | Evidence |" in text


def test_ledger_states_and_evidence_valid() -> None:
    rows = parse_ledger(_ledger_text())
    for item_id, (item, state, evidence) in rows.items():
        assert item, item_id
        assert state in ALLOWED_STATES, (item_id, state)
        assert evidence_problems(item_id, state, evidence) == [], (item_id, evidence)


GOOD = "| A1.9 | `github_pat_`/`xapp-` | already fixed | commit {sha} · test {test} |"
THIS_TEST = "tests/conformance/test_hardening_ledger.py::test_ledger_lists_every_item_once"


def _head() -> str:
    if not (ROOT / ".git").exists():
        pytest.skip("no git checkout (sdist)")
    out = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "--short=10", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def test_parse_ledger_rejects_bad_rows() -> None:
    sha = _head()
    good = GOOD.format(sha=sha, test=THIS_TEST)
    assert parse_ledger(good) == {
        "A1.9": ("`github_pat_`/`xapp-`", "already fixed", f"commit {sha} · test {THIS_TEST}")
    }
    assert parse_ledger("| A2.1 | path check | open | Task 4 |")["A2.1"][1] == "open"
    bad_rows = {
        "listed more than once": good + "\n" + good,
        "unknown state": "| A2.1 | path check | done | Task 4 |",
        "does not exist": GOOD.format(sha=sha, test="tests/nope/test_missing.py::test_x"),
        "defines no test": GOOD.format(sha=sha, test=THIS_TEST.replace("lists", "skips")),
        "not in the repository": GOOD.format(sha="0" * 12, test=THIS_TEST),
        "does not match": "| A2.1 | path check | open | later |",
        "at least 10 characters": "| A2.4 | SHOW | ruled out | no |",
    }
    for reason, text in bad_rows.items():
        with pytest.raises(ValueError, match=reason):
            parse_ledger(text)
