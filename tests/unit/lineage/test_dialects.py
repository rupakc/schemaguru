"""The lineage dialect table (0.2.0 spec 4.4)."""

from __future__ import annotations

import factlang.sql

from schemaguru.lineage.dialects import DBT_ADAPTERS, SQLGLOT_DIALECTS, sqlglot_dialect


def test_table_matches_factlang() -> None:
    assert dict(SQLGLOT_DIALECTS) == dict(factlang.sql.SQLGLOT_NAMES)


def test_dbt_adapter_aliases() -> None:
    assert sqlglot_dialect("postgres") == "postgres"
    assert sqlglot_dialect("bigquery") == "bigquery"
    for adapter, target in DBT_ADAPTERS.items():
        assert sqlglot_dialect(adapter) == target
    assert sqlglot_dialect("sqlserver") == "tsql"
    assert sqlglot_dialect("mariadb") == "mysql"


def test_dbt_adapter_targets_accepted() -> None:
    assert sqlglot_dialect("spark") == "spark"
    assert sqlglot_dialect("tsql") == "tsql"


def test_unknown_dialect_none() -> None:
    assert sqlglot_dialect("nonsense") is None
    assert sqlglot_dialect(None) is None
    assert sqlglot_dialect("") is None
