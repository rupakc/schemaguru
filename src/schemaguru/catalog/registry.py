"""Reader registry (spec 6): dialect name -> factory taking a metadata executor."""

from __future__ import annotations

from collections.abc import Callable

from schemaguru.catalog.executor import MetadataExecutor
from schemaguru.catalog.protocol import CatalogError, CatalogReader

ReaderFactory = Callable[[MetadataExecutor], CatalogReader]

_FACTORIES: dict[str, ReaderFactory] = {}


def register_reader(dialect: str, factory: ReaderFactory) -> None:
    _FACTORIES[dialect] = factory


def _load_builtin() -> None:
    from schemaguru.catalog.readers import bigquery as _bigquery  # noqa: F401
    from schemaguru.catalog.readers import clickhouse as _clickhouse  # noqa: F401
    from schemaguru.catalog.readers import databricks as _databricks  # noqa: F401
    from schemaguru.catalog.readers import duckdb as _duckdb  # noqa: F401
    from schemaguru.catalog.readers import mysql as _mysql  # noqa: F401
    from schemaguru.catalog.readers import oracle as _oracle  # noqa: F401
    from schemaguru.catalog.readers import postgres as _postgres  # noqa: F401
    from schemaguru.catalog.readers import redshift as _redshift  # noqa: F401
    from schemaguru.catalog.readers import snowflake as _snowflake  # noqa: F401
    from schemaguru.catalog.readers import trino as _trino  # noqa: F401
    from schemaguru.catalog.readers import tsql as _tsql  # noqa: F401


def dialects() -> list[str]:
    _load_builtin()
    return sorted(_FACTORIES)


def reader_for(dialect: str, executor: MetadataExecutor) -> CatalogReader:
    _load_builtin()
    try:
        factory = _FACTORIES[dialect]
    except KeyError:
        raise CatalogError(
            "SG101",
            f"no catalog reader for dialect {dialect!r}; "
            f"available: {', '.join(sorted(_FACTORIES))}",
        ) from None
    return factory(executor)
