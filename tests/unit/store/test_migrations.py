from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from schemaguru.errors import StoreVersionError
from schemaguru.store import schema_sql
from schemaguru.store.sqlite import SQLiteStore


def test_migrations_numbered_and_contiguous() -> None:
    ms = schema_sql.migrations()
    assert [v for v, _ in ms] == list(range(1, len(ms) + 1))
    assert len(ms) == schema_sql.LATEST_VERSION


def test_version_recorded_and_reopen(tmp_path: Path) -> None:
    p = tmp_path / "v.db"
    SQLiteStore(p).close()
    SQLiteStore(p).close()
    con = sqlite3.connect(p)
    rows = con.execute("SELECT version FROM schema_version ORDER BY version").fetchall()
    con.close()
    assert rows == [(v,) for v in range(1, schema_sql.LATEST_VERSION + 1)]


def test_newer_store_refused(tmp_path: Path) -> None:
    p = tmp_path / "n.db"
    SQLiteStore(p).close()
    con = sqlite3.connect(p)
    con.execute("INSERT INTO schema_version(version, applied_at) VALUES (999, 'x')")
    con.commit()
    con.close()
    with pytest.raises(StoreVersionError, match="SG702"):
        SQLiteStore(p)
    with pytest.raises(StoreVersionError, match="SG702"):
        SQLiteStore(p, readonly=True)


def test_readonly_on_unmigrated_refused(tmp_path: Path) -> None:
    p = tmp_path / "e.db"
    sqlite3.connect(p).close()
    with pytest.raises(StoreVersionError):
        SQLiteStore(p, readonly=True)


def test_codes_registered() -> None:
    from schemaguru.diagnostics import CODES

    assert "SG701" in CODES and "SG702" in CODES
