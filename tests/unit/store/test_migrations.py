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


def test_newer_store_refused_has_code(tmp_path: Path) -> None:
    p = tmp_path / "n.db"
    SQLiteStore(p).close()
    con = sqlite3.connect(p)
    con.execute("INSERT INTO schema_version(version, applied_at) VALUES (999, 'x')")
    con.commit()
    con.close()
    for readonly in (False, True):
        with pytest.raises(StoreVersionError) as info:
            SQLiteStore(p, readonly=readonly)
        assert info.value.code == "SG702"
    assert StoreVersionError("x").code == "SG702"


def test_readonly_on_unmigrated_refused(tmp_path: Path) -> None:
    p = tmp_path / "e.db"
    sqlite3.connect(p).close()
    with pytest.raises(StoreVersionError):
        SQLiteStore(p, readonly=True)


def test_codes_registered() -> None:
    from schemaguru.diagnostics import CODES

    assert "SG701" in CODES and "SG702" in CODES


def _store_at(path: Path, version: int) -> None:
    """A store file at schema ``version``, built from the shipped migrations 1..version."""
    con = sqlite3.connect(path)
    con.execute(schema_sql.VERSION_DDL)
    for v, sql in schema_sql.migrations()[:version]:
        for stmt in schema_sql.statements(sql):
            con.execute(stmt)
        con.execute("INSERT INTO schema_version(version, applied_at) VALUES (?, 'x')", (v,))
    con.commit()
    con.close()


def _lineage_row() -> tuple[str, str, str, str]:
    from schemaguru._util.hashing import canonical_json
    from schemaguru.model.facts import Fact, Provenance, Status
    from schemaguru.model.kinds import FactKind
    from schemaguru.model.lineage import table_lineage_value
    from schemaguru.model.refs import ObjectRef

    f = Fact.make(
        FactKind.LINEAGE,
        ObjectRef(None, "shop", "v"),
        table_lineage_value(["shop.orders"]),
        Provenance.DECLARED,
        Status.UNVERIFIED,
        source="dbt:manifest:model.shop.v",
    )
    return (f.id, f.kind.name, "shop.v", canonical_json(f.to_dict()))


def test_v2_store_migrates_to_3(tmp_path: Path) -> None:
    p = tmp_path / "v2.db"
    _store_at(p, 2)
    # a lineage fact written before the index existed gets its edges on migration
    row = _lineage_row()
    con = sqlite3.connect(p)
    con.execute("INSERT INTO facts(id, kind, subject_key, data) VALUES (?,?,?,?)", row)
    con.commit()
    con.close()
    with SQLiteStore(p) as s:
        versions = [r[0] for r in s._q("SELECT version FROM schema_version ORDER BY version")]
        assert versions == [1, 2, 3]
        edges = s.lineage_edges(["shop.orders"], direction="downstream", level="table")
        assert [(e.downstream_key, e.fact_id) for e in edges] == [("shop.v", row[0])]


def test_readonly_v2_store_refused_until_migrated(tmp_path: Path) -> None:
    p = tmp_path / "v2.db"
    _store_at(p, 2)
    with pytest.raises(StoreVersionError, match="open it read-write once"):
        SQLiteStore(p, readonly=True)
    SQLiteStore(p).close()
    with SQLiteStore(p, readonly=True) as s:
        assert s.lineage_edges(["shop.v"], direction="upstream", level="table") == []
