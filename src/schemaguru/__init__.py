"""SchemaGuru: automated schema intelligence for data warehouses.

Three lines from install to an agent-ready context (spec 2 G1)::

    import schemaguru as sg
    guru = sg.Guru.connect("duckdb:///shop.duckdb")
    print(guru.context("monthly revenue by customer segment"))
"""

from schemaguru import catalog, draft, graph, guard, measure, overlays, pii, render, scope
from schemaguru.__about__ import __version__
from schemaguru.config import Budget, Config, CrawlConfig, GraphConfig, SamplingConfig, ScopeConfig
from schemaguru.diagnostics import Diagnostic
from schemaguru.errors import SchemaGuruError
from schemaguru.graph.search import PathResult, Refusal
from schemaguru.guru import Guru
from schemaguru.knowledge.lineage import LineageResult
from schemaguru.model import Fact, FactKind, JoinRef, ObjectRef, Provenance, Status
from schemaguru.refresh import RefreshReport
from schemaguru.render import ContextView

# the public API, grouped as in the spec (19); tests/unit/test_guru.py pins it exactly
__all__ = [  # noqa: RUF022
    "Guru",
    "Config",
    "Budget",
    "SamplingConfig",
    "GraphConfig",
    "ScopeConfig",
    "CrawlConfig",
    "ObjectRef",
    "JoinRef",
    "Fact",
    "FactKind",
    "Provenance",
    "Status",
    "ContextView",
    "PathResult",
    "Refusal",
    "RefreshReport",
    "Diagnostic",
    "SchemaGuruError",
    "catalog",
    "measure",
    "draft",
    "render",
    "scope",
    "graph",
    "pii",
    "overlays",
    "guard",
    "LineageResult",
    "__version__",
]
