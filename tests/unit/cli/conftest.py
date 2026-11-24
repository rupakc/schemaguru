"""CLI test helpers: the shop warehouse as a DuckDB file URL and an in-process ``run``."""

from __future__ import annotations

import io
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from schemaguru.cli import main
from schemaguru.guru import Guru
from tests.fixtures.shop import build_shop
from tests.unit.measure.fake_backend import FakeBackend

duckdb = pytest.importorskip("duckdb")

Run = Callable[..., tuple[int, str, str]]


@pytest.fixture(scope="module")
def shop_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("wh") / "warehouse.duckdb"
    conn = duckdb.connect(str(path))
    build_shop(conn, "duckdb")
    conn.close()
    return path


@pytest.fixture
def url(shop_file: Path) -> str:
    return f"duckdb:///{shop_file}"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import os

    for k in list(os.environ):
        if k.startswith("SCHEMAGURU_"):
            monkeypatch.delenv(k)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def store(tmp_path: Path) -> str:
    return str(tmp_path / "s.db")


@pytest.fixture
def run(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> Run:
    """``run(*argv, stdin="")`` -> ``(exit code, stdout, stderr)`` of one ``main`` call."""

    def _run(*argv: str, stdin: str = "") -> tuple[int, str, str]:
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
        code = main(list(argv))
        cap = capsys.readouterr()
        return code, cap.out, cap.err

    return _run


@pytest.fixture
def fake_backend(monkeypatch: pytest.MonkeyPatch, shop_file: Path) -> Any:
    """Every Guru built from a URL measures through the DuckDB-backed fake."""
    conn = duckdb.connect(str(shop_file))
    monkeypatch.setattr(Guru, "_default_backend", lambda self: FakeBackend(conn))
    yield conn
    conn.close()


def jloads(out: str) -> Any:
    return json.loads(out)
