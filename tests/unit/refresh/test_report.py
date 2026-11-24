from __future__ import annotations

import json
from typing import Any

from schemaguru.store.sqlite import SQLiteStore
from tests.unit.refresh.test_run import go


def test_report_json_and_markdown(store: SQLiteStore, conn: Any) -> None:
    go(store, conn)
    conn.execute("DROP TABLE shop.orders")
    r = go(store, conn)
    d = json.loads(r.to_json())
    assert d["exit_code"] == 1 and d["counts"]["REVIEW"] >= 1
    assert any(c["kind"] == "TABLE_WITHDRAWN" and c["class"] == "REVIEW" for c in d["changes"])
    md = r.to_markdown()
    assert md.startswith("# Refresh report") and "Needs review" in md


def test_exit3_markdown_says_what_happened(store: SQLiteStore, conn: Any) -> None:
    from schemaguru.measure.requests import Uniqueness

    # errors while tables WERE measured: must not claim nothing was measured
    r = go(store, conn, fail=lambda req: isinstance(req, Uniqueness))
    assert r.exit_code == 3 and r.measure.measured_tables
    md = r.to_markdown()
    assert "Errors occurred during the run (" in md and "nothing was measured" not in md.lower()


def test_exit3_markdown_nothing_measured(store: SQLiteStore, conn: Any) -> None:
    from schemaguru.config import Budget, Config

    cfg = Config(budget=Budget(max_bytes_total=0, max_bytes_per_probe=None))
    r = go(store, conn, cfg, cost=lambda req: 400)
    assert r.exit_code == 3 and not r.measure.measured_tables
    md = r.to_markdown()
    assert "errors occurred during the run (" in md.lower() and "nothing was measured" in md
