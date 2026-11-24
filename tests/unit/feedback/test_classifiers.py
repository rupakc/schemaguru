from __future__ import annotations

from schemaguru.feedback.classifiers import BUILTIN_CLASSIFIERS, FeedbackRecord, classify_all
from schemaguru.model import ObjectRef


def run(sql: str, err: str, dialect: str | None = None) -> list[FeedbackRecord]:
    return classify_all(sql, err, dialect)


ORD = ObjectRef(None, None, "orders")


def test_type_mismatch_bigquery_style() -> None:
    sql = "SELECT id\nFROM orders\n  WHERE created_at = 5"
    # line 3, col 9 is the start of created_at
    err = "No matching signature for operator = for argument types: STRING, INT64 at [3:9]"
    assert run(sql, err) == [FeedbackRecord(ORD, "created_at", "type_mismatch")]


def test_type_mismatch_alias_resolved() -> None:
    sql = "SELECT 1 FROM orders o JOIN customers c ON c.id = o.customer_id WHERE o.created_at = 5"
    pos = sql.index("o.created_at") + 1
    err = f"No matching signature for operator = for argument types: STRING, INT64 at [1:{pos}]"
    assert run(sql, err) == [FeedbackRecord(ORD, "created_at", "type_mismatch")]


def test_bare_column_unattributed_with_two_tables() -> None:
    sql = "SELECT 1 FROM orders o JOIN customers c ON 1=1 WHERE created_at = 5"
    pos = sql.index("created_at") + 1
    err = f"No matching signature for operator = for argument types: STRING, INT64 at [1:{pos}]"
    assert run(sql, err) == []


def test_postgres_style_operator_error() -> None:
    sql = "SELECT * FROM orders WHERE created_at = 5"
    err = (
        "operator does not exist: text = integer\n"
        f"LINE 1: {sql}\n"
        + " " * (len("LINE 1: ") + sql.index("="))
        + "^\nHINT: No operator matches the given name and argument types."
    )
    assert run(sql, err) == [FeedbackRecord(ORD, "created_at", "type_mismatch")]
    err2 = f"ERROR: operator does not exist: text = integer at character {sql.index('=') + 1}"
    assert run(sql, err2) == [FeedbackRecord(ORD, "created_at", "type_mismatch")]


def test_literal_not_attributed() -> None:
    sql = "SELECT * FROM orders WHERE 'created_at' = 5"
    pos = sql.index("'created_at'") + 1
    err = f"No matching signature for operator = for argument types: STRING, INT64 at [1:{pos}]"
    assert run(sql, err) == []


def test_numeric_pair_ignored() -> None:
    sql = "SELECT * FROM orders WHERE total = 5"
    pos = sql.index("total") + 1
    err = f"No matching signature for operator = for argument types: INT64, FLOAT64 at [1:{pos}]"
    assert run(sql, err) == []


def test_unattributed_refused() -> None:
    assert run("SELECT 1", "No matching signature for operator = for argument types: A, B") == []
    assert run("SELECT 1", "something unrelated happened") == []
    assert run("", "") == []


def test_unknown_column_variants() -> None:
    sql = "SELECT nope FROM orders"
    exp = [FeedbackRecord(ORD, "nope", "unknown_column")]
    assert run(sql, 'ERROR: column "nope" does not exist\nLINE 1: SELECT nope') == exp
    assert run(sql, "Unrecognized name: nope at [1:8]") == exp
    assert run(sql, "Unknown column 'nope' in 'field list'") == exp
    assert run(sql, "no such column: nope") == exp
    assert run("SELECT o.nope FROM orders o", "column o.nope does not exist") == exp
    assert run("SELECT 1", 'column "nope" of relation "orders" does not exist') == exp


def test_unknown_table_and_ambiguous() -> None:
    assert run("SELECT 1 FROM shop.ordrs", 'relation "shop.ordrs" does not exist') == [
        FeedbackRecord(ObjectRef(None, "shop", "ordrs"), None, "unknown_table")
    ]
    assert run("SELECT 1", "Not found: Table p:shop.ordrs was not found in location EU") == [
        FeedbackRecord(ObjectRef("p", "shop", "ordrs"), None, "unknown_table")
    ]
    recs = run(
        "SELECT id FROM orders o JOIN customers c ON 1=1", 'column reference "id" is ambiguous'
    )
    assert {(r.table.table if r.table else None, r.column, r.failure_class) for r in recs} == {
        ("orders", "id", "ambiguous_column"),
        ("customers", "id", "ambiguous_column"),
    }


def test_builtin_registry_nonempty() -> None:
    assert len(BUILTIN_CLASSIFIERS) >= 4
