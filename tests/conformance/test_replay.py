"""The replay harness itself, with negative controls."""

from __future__ import annotations

import pytest

from schemaguru.catalog.readers.duckdb import DuckDBReader
from tests.conformance.replay import ReplayExecutor, UnknownSQL, fingerprint, read_all


def test_fingerprint_collapses_whitespace_and_case() -> None:
    assert fingerprint("SELECT  a\n FROM   T ") == "select a from t"


def test_replay_matches_on_sql_and_params() -> None:
    ex = ReplayExecutor([{"sql": "select 1 where x = ?", "params": ["a"], "rows": [{"n": 1}]}])
    assert ex.run("SELECT 1\n WHERE x = ?", ["a"]) == [{"n": 1}]
    with pytest.raises(UnknownSQL):
        ex.run("select 1 where x = ?", ["b"])


def test_unknown_sql_fails_and_prints_the_fingerprint() -> None:
    ex = ReplayExecutor([])
    with pytest.raises(UnknownSQL, match="select 2 from t"):
        ex.run("SELECT   2 FROM t")


def test_a_reader_with_an_empty_fixture_fails_not_passes() -> None:
    with pytest.raises(UnknownSQL):
        read_all(DuckDBReader(ReplayExecutor([])))
