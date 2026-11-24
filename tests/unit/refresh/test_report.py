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
