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


def test_a_stale_recording_is_reported() -> None:
    ex = ReplayExecutor(
        [
            {"sql": "select 1", "params": [], "rows": []},
            {"sql": "select 2", "params": [], "rows": []},
        ]
    )
    ex.run("select 1")
    with pytest.raises(AssertionError, match="stale"):
        ex.assert_all_consumed()
    ex.run("select 2")
    ex.assert_all_consumed()


def test_scripted_rejects_unknown_statement() -> None:
    """A synthetic-dialect handler answers only the statements it was written for. Matching SQL
    by substring let a reader's changed (or new) query be answered by whatever branch happened
    to share a keyword, so a fixture could stay green for SQL no engine would accept."""
    from schemaguru.catalog import reader_for
    from tests.conformance.record_warehouses import (
        KNOWN,
        SCRIPTS,
        Scripted,
        UnscriptedStatement,
        digest,
    )

    assert set(KNOWN) == set(SCRIPTS)
    for dialect in SCRIPTS:
        # every shipped reader's statements are all known, and no known digest is left over
        engine = Scripted(SCRIPTS[dialect], KNOWN[dialect])
        read_all(reader_for(dialect, engine))
        assert set(engine.seen) == set(KNOWN[dialect]), dialect

        listing = next(sql for sql in engine.seen.values() if sql.startswith("select"))
        changed = listing.replace("select", "select 1 as extra,", 1)
        with pytest.raises(UnscriptedStatement, match="not written for this statement"):
            engine.run(changed)
        # whitespace and case are not a change
        assert digest("  " + listing.upper().replace(" ", "\n  ")) == digest(listing)
    bq = Scripted(SCRIPTS["bigquery"], KNOWN["bigquery"])
    with pytest.raises(UnscriptedStatement):  # shares a keyword with a branch ("schemata")
        bq.run("SELECT 1 FROM `region-us`.INFORMATION_SCHEMA.SCHEMATA")
    # negative control: without a known set the handler would have answered by keyword
    assert Scripted(SCRIPTS["bigquery"]).run(
        "SELECT 1 FROM `region-us`.information_schema.schemata"
    )
