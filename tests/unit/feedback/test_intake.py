from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from schemaguru.feedback import report_error
from schemaguru.feedback.classifiers import FeedbackRecord
from schemaguru.model import ObjectRef
from schemaguru.store.sqlite import SQLiteStore


def test_report_error_records_identifiers_only(shop_store: Any) -> None:
    sql = "SELECT * FROM orders WHERE created_at = 'SECRET-LITERAL'"
    pos = sql.index("created_at") + 1
    err = (
        "No matching signature for operator = for argument types: INT64, STRING "
        f"at [1:{pos}] SECRET-ERR"
    )
    assert report_error(shop_store, sql, err) == 1
    summary = shop_store.feedback_summary()
    assert summary == [
        {
            "table": "shop.orders",
            "column": "created_at",
            "failure_class": "type_mismatch",
            "count": 1,
        }
    ]
    assert "SECRET" not in repr(summary)
    assert shop_store.queue_pop(5) == [
        (ObjectRef(None, "shop", "orders"), "feedback:type_mismatch")
    ]


def test_report_error_unresolvable_table_skipped(shop_store: Any) -> None:
    assert (
        report_error(shop_store, "SELECT 1 FROM nowhere", 'relation "nowhere" does not exist') == 0
    )
    assert shop_store.feedback_summary() == []


def test_report_error_ambiguous_keeps_only_tables_holding_column(shop_store: Any) -> None:
    n = report_error(
        shop_store,
        "SELECT order_id FROM orders o JOIN customers c ON 1=1",
        'column reference "order_id" is ambiguous',
    )
    tables = {r["table"] for r in shop_store.feedback_summary()}
    assert n == len(tables)
    assert "shop.customers" not in tables


@pytest.mark.parametrize(
    "sql,err", [(None, None), (b"\xff", 5), ("\x00" * 10, "\x00"), ("'" * 99999, "[:]")]
)
def test_report_error_never_raises(shop_store: Any, sql: Any, err: Any) -> None:
    assert report_error(shop_store, sql, err) == 0


def test_report_error_survives_broken_store_and_classifier(tmp_path: Path) -> None:
    class Boom:
        def classify(self, sql: str, error: str, dialect: str | None) -> list[FeedbackRecord]:
            raise RuntimeError("x")

    with SQLiteStore(tmp_path / "s.db") as store:
        assert report_error(store, "SELECT 1", "no such column: a", classifiers=[Boom()]) == 0

    class BrokenStore:
        def resolve(self, text: str) -> Any:
            raise RuntimeError("down")

    assert report_error(BrokenStore(), "SELECT a FROM t", "no such column: a") == 0  # type: ignore[arg-type]


def test_reprobe_priority_accumulates(shop_store: Any) -> None:
    for _ in range(3):
        assert report_error(shop_store, "SELECT nope FROM orders", "no such column: nope") == 1
    (prio,) = [r[0] for r in shop_store._conn.execute("SELECT priority FROM queue")]
    assert prio >= 3.0


def test_intake_refuses_non_identifier_column_from_custom_classifier(shop_store: Any) -> None:
    class Evil:
        def classify(self, sql: str, error: str, dialect: str | None) -> list[FeedbackRecord]:
            ref = ObjectRef(None, "shop", "customers")
            return [FeedbackRecord(ref, "Jane Doe", "unknown_column")]

    assert report_error(shop_store, "SELECT 1", "x", classifiers=[Evil()]) == 0
    assert shop_store.feedback_summary() == []
