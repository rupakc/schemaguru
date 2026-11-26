"""The reused, killable parser worker (0.2.0 spec 4.4)."""

from __future__ import annotations

import gc
import os
import subprocess
import sys
import textwrap
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("sqlglot")

from schemaguru.lineage.parse import ColumnLineage, LineageFailure, ParsedLineage
from schemaguru.lineage.worker import (
    DEFAULT_TIMEOUT_S,
    ExtractRequest,
    LineageRequest,
    ParserPool,
    WorkerResult,
)
from schemaguru.model.lineage import ResolvedTable

REPO = Path(__file__).resolve().parents[3]
HOSTILE = [sys.executable, "-m", "tests.unit.lineage.hostile_worker"]


def _plain(results: Sequence[WorkerResult]) -> list[Any]:
    return [r.reason if isinstance(r, LineageFailure) else r for r in results]


@pytest.fixture
def hostile(monkeypatch: pytest.MonkeyPatch) -> Iterator[ParserPool]:
    monkeypatch.chdir(REPO)
    with ParserPool(timeout_s=0.5, command=HOSTILE) as pool:
        yield pool


def test_default_timeout_is_five_seconds() -> None:
    assert DEFAULT_TIMEOUT_S == 5.0


def test_timeout_kills_and_replaces(hostile: ParserPool) -> None:
    start = time.monotonic()
    out = hostile.map(
        [
            ExtractRequest("SELECT 1 FROM a", "duckdb"),
            ExtractRequest("HANG", "duckdb"),
            ExtractRequest("SELECT 1 FROM b", "duckdb"),
        ]
    )
    elapsed = time.monotonic() - start
    assert _plain(out) == [((None, None, "a"),), "timeout", ((None, None, "b"),)]
    assert hostile.restarts == 1
    assert elapsed < 5


def test_death_reported_and_replaced(hostile: ParserPool) -> None:
    out = hostile.map(
        [ExtractRequest("DIE", "duckdb"), ExtractRequest("SELECT 1 FROM b", "duckdb")]
    )
    assert _plain(out) == ["worker_died", ((None, None, "b"),)]
    assert hostile.restarts == 1


def test_garbage_reply_is_worker_died(hostile: ParserPool) -> None:
    out = hostile.map(
        [ExtractRequest("GARBAGE", "duckdb"), ExtractRequest("SELECT 1 FROM b", "duckdb")]
    )
    assert _plain(out) == ["worker_died", ((None, None, "b"),)]
    assert hostile.restarts == 1


def test_parse_error_is_not_a_restart(hostile: ParserPool) -> None:
    out = hostile.map([ExtractRequest("SELECT FROM (", "duckdb")])
    assert _plain(out) == ["parse_error"]
    assert hostile.restarts == 0


def test_too_large_not_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(REPO)
    # "HANG" would time out if it were sent; over the limit it never is
    with ParserPool(timeout_s=0.5, max_chars=3, command=HOSTILE) as pool:
        assert _plain(pool.map([ExtractRequest("HANG", "duckdb")])) == ["too_large"]
        assert pool.restarts == 0
        assert pool._proc is None  # nothing was ever started


def test_at_limit_is_sent() -> None:
    sql = "SELECT 1 FROM a"
    with ParserPool(max_chars=len(sql)) as pool:
        assert _plain(pool.map([ExtractRequest(sql, "duckdb")])) == [((None, None, "a"),)]
    with ParserPool(max_chars=len(sql) - 1) as pool:
        assert _plain(pool.map([ExtractRequest(sql, "duckdb")])) == ["too_large"]


def test_unsupported_dialect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(REPO)
    with ParserPool(timeout_s=0.5, command=HOSTILE) as pool:
        assert _plain(pool.map([ExtractRequest("HANG", "no_such_dialect")])) == [
            "unsupported_dialect"
        ]
        assert pool._proc is None


def test_startup_paid_once() -> None:
    with ParserPool() as pool:
        first = pool.map([ExtractRequest("SELECT 1 FROM t0", "duckdb")])
        assert first == [((None, None, "t0"),)]
        assert pool._proc is not None
        pid = pool._proc.pid
        rest = pool.map([ExtractRequest(f"SELECT 1 FROM t{i}", "duckdb") for i in range(1, 50)])
        assert rest == [((None, None, f"t{i}"),) for i in range(1, 50)]
        assert pool._proc is not None and pool._proc.pid == pid
        assert pool.restarts == 0


def test_real_worker_parses() -> None:
    schema = {(None, "shop", "orders"): ResolvedTable("shop.orders", ("id", "amount"))}
    with ParserPool() as pool:
        out = pool.map(
            [
                ExtractRequest("SELECT id FROM shop.orders", "postgres"),
                LineageRequest("SELECT id, amount * 2 AS a2 FROM shop.orders", "postgres", schema),
                LineageRequest("SELECT id FROM shop.orders", "postgres", schema, columns=False),
            ]
        )
    assert out[0] == ((None, "shop", "orders"),)
    assert out[1] == ParsedLineage(
        upstream=("shop.orders",),
        unresolved=(),
        columns=(
            ColumnLineage("a2", ("shop.orders.amount",), "expression", ()),
            ColumnLineage("id", ("shop.orders.id",), "copy", ()),
        ),
    )
    assert isinstance(out[2], ParsedLineage) and out[2].columns == ()


def test_close_reaps_the_child() -> None:
    pool = ParserPool()
    pool.map([ExtractRequest("SELECT 1 FROM a", "duckdb")])
    proc = pool._proc
    assert proc is not None
    pool.close()
    assert proc.returncode is not None
    assert pool._proc is None
    pool.close()  # idempotent


def test_empty_map_starts_nothing() -> None:
    with ParserPool() as pool:
        assert pool.map([]) == []
        assert pool._proc is None


def test_worker_from_unguarded_script(tmp_path: Path) -> None:
    script = tmp_path / "unguarded.py"
    script.write_text(
        textwrap.dedent(
            """\
            from schemaguru.lineage.worker import ExtractRequest, ParserPool

            pool = ParserPool()
            print(pool.map([ExtractRequest("SELECT 1 FROM a", "duckdb")]))
            pool.close()
            """
        ),
        encoding="utf-8",
    )
    done = subprocess.run(
        [sys.executable, str(script)], timeout=60, capture_output=True, text=True, check=False
    )
    assert done.returncode == 0
    assert done.stdout.strip() == "[((None, None, 'a'),)]"


def test_blocked_write_is_a_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    # the child stops reading: a ~1 MB request blocks on the pipe and shares the request's cap
    monkeypatch.chdir(REPO)
    sql = "SELECT 1 FROM a WHERE x = '" + "y" * 990_000 + "'"
    with ParserPool(timeout_s=0.5, command=[*HOSTILE, "noread"]) as pool:
        start = time.monotonic()
        out = pool.map([ExtractRequest(sql, "duckdb")])
        elapsed = time.monotonic() - start
        assert _plain(out) == ["timeout"]
        assert pool.restarts == 1 and pool._proc is None
    assert elapsed < 0.5 + 3.0


def test_endless_reply_line_is_worker_died(hostile: ParserPool) -> None:
    out = hostile.map([ExtractRequest("ENDLESS", "duckdb")])
    assert _plain(out) in (["worker_died"], ["timeout"])  # bounded either way, never a hang
    assert hostile.restarts == 1


def test_endless_reply_line_is_cut_at_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(REPO)
    monkeypatch.setattr("schemaguru.lineage.worker.MAX_REPLY_BYTES", 1 << 16)
    with ParserPool(timeout_s=30, command=HOSTILE) as pool:
        start = time.monotonic()
        assert _plain(pool.map([ExtractRequest("ENDLESS", "duckdb")])) == ["worker_died"]
        assert time.monotonic() - start < 15  # cut by size, not by the 30 s timeout
        # negative control: an ordinary reply fits under the same limit
        assert pool.map([ExtractRequest("SELECT 1 FROM b", "duckdb")]) == [((None, None, "b"),)]


def test_reply_of_the_wrong_kind_is_worker_died(hostile: ParserPool) -> None:
    out = hostile.map(
        [ExtractRequest("WRONGKIND", "duckdb"), ExtractRequest("SELECT 1 FROM b", "duckdb")]
    )
    assert _plain(out) == ["worker_died", ((None, None, "b"),)]
    assert hostile.restarts == 1


def test_garbage_collected_pool_reaps_its_child() -> None:
    pool = ParserPool()
    pool.map([ExtractRequest("SELECT 1 FROM a", "duckdb")])
    proc = pool._proc
    assert proc is not None
    del pool
    gc.collect()
    try:
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:  # never leave the child behind, even when the test fails
            proc.kill()
            proc.wait()
    assert proc.returncode is not None


def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/status", encoding="ascii") as fh:
            return not any(line.startswith("State:\tZ") for line in fh)
    except (FileNotFoundError, ProcessLookupError):  # gone, or went while being read
        return False


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="parent-death signal is Linux-only"
)
def test_child_dies_with_its_parent(tmp_path: Path) -> None:
    script = tmp_path / "parent.py"
    script.write_text(
        textwrap.dedent(
            """\
            import os, sys, threading, time
            from schemaguru.lineage.worker import ExtractRequest, ParserPool

            pool = ParserPool(command=[sys.executable, "-m", "tests.unit.lineage.hostile_worker"])
            threading.Thread(
                target=pool.map, args=([ExtractRequest("HANG", "duckdb")],), daemon=True
            ).start()
            while pool._proc is None:
                time.sleep(0.01)
            time.sleep(1.0)  # the child is inside HANG now
            print(pool._proc.pid, flush=True)
            os._exit(0)
            """
        ),
        encoding="utf-8",
    )
    done = subprocess.run(
        [sys.executable, str(script)], cwd=REPO, timeout=60, capture_output=True, text=True,
        check=False,
    )  # fmt: skip
    pid = int(done.stdout.strip())
    deadline = time.monotonic() + 10
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    alive = _alive(pid)
    if alive:
        os.kill(pid, 9)
    assert not alive
