"""Dialect names the lineage parser hands to ``sqlglot`` (0.2.0 spec 4.4).

The table is SchemaGuru's own: only the FactLang seam modules may import FactLang, so its
``SQLGLOT_NAMES`` is mirrored here (a test pins the two equal).
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

_NAMES = (
    "bigquery", "clickhouse", "databricks", "duckdb", "mysql", "oracle", "postgres", "redshift",
    "snowflake", "trino", "tsql",
)  # fmt: skip

# SchemaGuru / FactLang dialect name -> sqlglot dialect name.
SQLGLOT_DIALECTS: Mapping[str, str] = MappingProxyType({n: n for n in _NAMES})

# dbt adapter type -> sqlglot dialect name (adapters whose name is not a dialect name).
DBT_ADAPTERS: Mapping[str, str] = MappingProxyType(
    {"sqlserver": "tsql", "synapse": "tsql", "fabric": "tsql", "mariadb": "mysql", "spark": "spark"}
)

_TARGETS = frozenset(DBT_ADAPTERS.values())


def sqlglot_dialect(name: str | None) -> str | None:
    """The sqlglot dialect for ``name`` (a dialect name, a dbt adapter or an adapter's target),
    or ``None`` when it is not supported."""
    if not name:
        return None
    if name in SQLGLOT_DIALECTS:
        return SQLGLOT_DIALECTS[name]
    if name in DBT_ADAPTERS:
        return DBT_ADAPTERS[name]
    if name in _TARGETS:
        return name
    return None
