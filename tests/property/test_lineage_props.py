"""``parse_lineage`` / ``extract_tables`` raise nothing but ``LineageFailure`` (0.2.0 spec 4.4)."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

pytest.importorskip("sqlglot")

from schemaguru.lineage.parse import LineageFailure, extract_tables, parse_lineage
from schemaguru.model.lineage import ResolvedTable

_TOKENS = [
    "SELECT", "FROM", "WHERE", "WITH", "RECURSIVE", "AS", "UNION", "ALL", "INTERSECT", "EXCEPT",
    "JOIN", "LEFT", "ON", "LATERAL", "UNNEST", "QUALIFY", "OVER", "PARTITION", "BY", "ORDER",
    "GROUP", "CAST", "TRY_CAST", "::", "SUM", "COUNT", "(", ")", ",", ".", "*", ";", "'x'", "1",
    "a", "b", "t", "s.t", "o", "CREATE", "VIEW", "v", "OR", "REPLACE", "=", "+", "--", "/*", "*/",
    "\"q\"", "`b`", "[c]", "$$", "\\", "\n", "ROW_NUMBER()", "CASE", "WHEN", "THEN", "END",
]  # fmt: skip

_SCHEMA = {
    (None, "s", "t"): ResolvedTable("s.t", ("a", "b")),
    (None, None, "t"): ResolvedTable("t", ("a",)),
}

fragments = (
    st.lists(st.one_of(st.sampled_from(_TOKENS), st.text(max_size=6)), max_size=40)
    .map(" ".join)
    .filter(lambda s: len(s) <= 200)
)


@settings(deadline=None, max_examples=300)
@given(
    sql=fragments,
    dialect=st.sampled_from(["postgres", "snowflake", "bigquery", "tsql", "clickhouse", "nope"]),
)
def test_never_raises_other_than_failure(sql: str, dialect: str) -> None:
    for call in (
        lambda: extract_tables(sql, dialect),
        lambda: parse_lineage(sql, dialect, _SCHEMA),
        lambda: parse_lineage(sql, dialect, _SCHEMA, columns=False),
    ):
        try:
            call()
        except LineageFailure as exc:
            assert str(exc) in {"parse_error", "too_large", "unsupported_dialect"}
