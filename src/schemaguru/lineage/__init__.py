"""Table and column lineage parsed from SQL definitions (0.2.0 spec 4.4).

``sqlglot`` is an optional dependency (extra ``lineage``), imported only inside functions.
"""

from __future__ import annotations

from schemaguru.lineage.dialects import DBT_ADAPTERS, SQLGLOT_DIALECTS, sqlglot_dialect
from schemaguru.lineage.parse import (
    MAX_DEFINITION_CHARS,
    ColumnLineage,
    FailureReason,
    LineageFailure,
    ParsedLineage,
    extract_tables,
    lineage_available,
    parse_lineage,
)

__all__ = [
    "DBT_ADAPTERS",
    "MAX_DEFINITION_CHARS",
    "SQLGLOT_DIALECTS",
    "ColumnLineage",
    "FailureReason",
    "LineageFailure",
    "ParsedLineage",
    "extract_tables",
    "lineage_available",
    "parse_lineage",
    "sqlglot_dialect",
]
