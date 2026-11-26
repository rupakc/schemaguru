"""Table and column lineage parsed from SQL definitions (0.2.0 spec 4.4).

``sqlglot`` is an optional dependency (extra ``lineage``), imported only inside functions.
The worker and analysis names load on first use, so ``python -m schemaguru.lineage.worker``
does not import its own module twice.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

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

if TYPE_CHECKING:
    from schemaguru.lineage.analyse import (
        DefinitionItem,
        DefinitionResult,
        Resolver,
        analyse_definitions,
        extra_missing_diagnostic,
        failure_diagnostic,
        lineage_facts,
    )
    from schemaguru.lineage.worker import (
        DEFAULT_TIMEOUT_S,
        ExtractRequest,
        LineageRequest,
        ParserPool,
        WorkerResult,
    )

_LAZY = {
    "DefinitionItem": "analyse",
    "DefinitionResult": "analyse",
    "Resolver": "analyse",
    "analyse_definitions": "analyse",
    "extra_missing_diagnostic": "analyse",
    "failure_diagnostic": "analyse",
    "lineage_facts": "analyse",
    "DEFAULT_TIMEOUT_S": "worker",
    "ExtractRequest": "worker",
    "LineageRequest": "worker",
    "ParserPool": "worker",
    "WorkerResult": "worker",
}


def __getattr__(name: str) -> Any:
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(f"{__name__}.{module}"), name)


__all__ = [
    "DBT_ADAPTERS",
    "DEFAULT_TIMEOUT_S",
    "MAX_DEFINITION_CHARS",
    "SQLGLOT_DIALECTS",
    "ColumnLineage",
    "DefinitionItem",
    "DefinitionResult",
    "ExtractRequest",
    "FailureReason",
    "LineageFailure",
    "LineageRequest",
    "ParsedLineage",
    "ParserPool",
    "Resolver",
    "WorkerResult",
    "analyse_definitions",
    "extra_missing_diagnostic",
    "extract_tables",
    "failure_diagnostic",
    "lineage_available",
    "lineage_facts",
    "parse_lineage",
    "sqlglot_dialect",
]
