"""Conformance: every registered reader with a fixture dir reproduces its ``expected.json``."""

from __future__ import annotations

import json

import pytest

from schemaguru.catalog import dialects, reader_for
from tests.conformance.replay import FIXTURES, ReplayExecutor, read_all

FIXTURE_DIALECTS = [d for d in dialects() if (FIXTURES / d / "queries.json").is_file()]


def test_every_fixture_dir_is_a_registered_reader() -> None:
    on_disk = {p.parent.name for p in FIXTURES.glob("*/queries.json")}
    assert on_disk <= set(dialects()), on_disk - set(dialects())


@pytest.mark.parametrize("dialect", FIXTURE_DIALECTS)
def test_reader_matches_recorded_fixture(dialect: str) -> None:
    executor = ReplayExecutor.from_fixture(dialect)
    got = read_all(reader_for(dialect, executor))
    expected = json.loads((FIXTURES / dialect / "expected.json").read_text(encoding="utf-8"))
    assert got == expected


@pytest.mark.parametrize("dialect", FIXTURE_DIALECTS)
def test_fixture_covers_the_shapes_every_reader_must_handle(dialect: str) -> None:
    expected = json.loads((FIXTURES / dialect / "expected.json").read_text(encoding="utf-8"))
    assert (
        len({t["ref"]["schema"] for t in expected["tables"]}) >= 2 or len(expected["schemas"]) >= 2
    )
    assert len(expected["tables"]) >= 5
    assert any(t["kind"] == "view" for t in expected["tables"])
    composite = [c for c in expected["constraints"] if c["kind"] == "primary_key"]
    assert any(len(c["columns"]) >= 2 for c in composite)
    assert any(t["comment"] for t in expected["tables"]) or any(
        c["comment"] for c in expected["columns"]
    )
