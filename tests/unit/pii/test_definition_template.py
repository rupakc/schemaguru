"""``definition_template``: the stored form of a view definition (0.2.0 spec 4.4, F4)."""

from __future__ import annotations

from schemaguru.pii.mask import TEMPLATE_MAX_CHARS, definition_template


def test_literals_replaced() -> None:
    sql = "CREATE VIEW v AS SELECT * FROM c WHERE email = 'a@b.co' AND id = 4242 AND t1.col2 > 1e5"
    assert definition_template(sql) == (
        "CREATE VIEW v AS SELECT * FROM c WHERE email = ? AND id = ? AND t1.col2 > ?"
    )


def test_numbers_in_identifiers_and_hex_kept_or_masked() -> None:
    assert definition_template("SELECT col_2, 0x1F, 3.25, -7 FROM t9") == (
        "SELECT col_2, ?, ?, -? FROM t9"
    )
    # a quoted identifier holding digits is an identifier, not a literal
    assert definition_template('SELECT "c 42" FROM t') == 'SELECT "c 42" FROM t'


def test_unsure_or_long_is_none() -> None:
    assert definition_template("SELECT 'open literal FROM t") is None
    assert definition_template("SELECT a FROM t WHERE x = $$a$$ OR y = 'b\\'c'") is None
    long_sql = "SELECT " + ", ".join(f"c{i}" for i in range(TEMPLATE_MAX_CHARS // 4)) + " FROM t"
    assert len(long_sql) > TEMPLATE_MAX_CHARS
    assert definition_template(long_sql) is None
