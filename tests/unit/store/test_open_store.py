"""``store=`` / ``Config.store_path`` given a URL: a Postgres DSN opens ``PostgresStore``; any
other URL is a ``ConfigError``; neither ever becomes a SQLite file named after the URL."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, ClassVar

import pytest

import schemaguru.store.postgres as pg_mod
from schemaguru.config import Config
from schemaguru.errors import ConfigError
from schemaguru.guru import Guru
from schemaguru.store.open import open_store
from schemaguru.store.sqlite import SQLiteStore

DSN = "postgresql://sg:hunter2pw@db.example:5432/knowledge"


class _FakePG(SQLiteStore):
    """Stands in for PostgresStore (no server in the unit job): records the DSN it got."""

    opened: ClassVar[list[str]] = []

    def __init__(self, dsn: str, **_: Any) -> None:
        type(self).opened.append(dsn)
        super().__init__(":memory:")


@pytest.fixture
def fake_pg(monkeypatch: pytest.MonkeyPatch) -> type[_FakePG]:
    _FakePG.opened = []
    monkeypatch.setattr(pg_mod, "PostgresStore", _FakePG)
    return _FakePG


def _files(root: Path) -> list[str]:
    return [str(p) for p in root.rglob("*")]


@pytest.mark.parametrize("dsn", [DSN, "postgres://sg:hunter2pw@db.example/k", DSN.upper()])
def test_postgres_dsn_opens_postgres_store(
    fake_pg: type[_FakePG], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dsn: str
) -> None:
    monkeypatch.chdir(tmp_path)
    for guru in (Guru(store=dsn), Guru(config=Config(store_path=dsn))):
        assert isinstance(guru.store, _FakePG)
        guru.close()
    assert fake_pg.opened == [dsn, dsn]
    assert _files(tmp_path) == []  # no SQLite file named after the DSN


@pytest.mark.parametrize(
    "url",
    [
        "mysql://sg:hunter2pw@db.example/k",
        "snowflake://u:hunter2pw@acct/db",
        "sqlite:///x.db",
        "file:///tmp/x.db",
    ],
)
def test_other_urls_are_a_config_error(
    fake_pg: type[_FakePG], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    monkeypatch.chdir(tmp_path)
    for make in (lambda: Guru(store=url), lambda: Guru(config=Config(store_path=url))):
        with pytest.raises(ConfigError) as info:
            make()
        assert "hunter2pw" not in str(info.value)
    assert fake_pg.opened == []
    assert _files(tmp_path) == []


def test_plain_path_is_still_sqlite(tmp_path: Path) -> None:
    """Negative control: a path (with or without a drive-like colon) is a SQLite file."""
    store = open_store(tmp_path / "k.db")
    assert isinstance(store, SQLiteStore)
    store.close()
    assert os.path.exists(tmp_path / "k.db")
