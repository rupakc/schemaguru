from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from schemaguru.model.refs import ObjectRef
from schemaguru.model.structure import Column, Table
from schemaguru.store.sqlite import SQLiteStore

FIXTURES = Path(__file__).resolve().parent.parent.parent / "fixtures"

SHOP: dict[str, list[str]] = {
    "orders": ["order_id", "customer_id", "status", "created_at", "amount"],
    "customers": ["customer_id", "email"],
    "payments": ["payment_id", "order_id"],
    "events": ["event_id", "session_id"],
    "sessions": ["session_id"],
    "orders_backup_2019": ["order_id"],
}


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SQLiteStore]:
    s = SQLiteStore(tmp_path / "t.db")
    for t, cols in SHOP.items():
        ref = ObjectRef(None, "shop", t)
        s.upsert_tables([Table(ref, "table")])
        s.upsert_columns(
            ref,
            [
                Column(ObjectRef(None, "shop", t, c), i, "STRING", "STRING", True, None, None)
                for i, c in enumerate(cols, 1)
            ],
        )
    yield s
    s.close()
