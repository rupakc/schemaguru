"""Measurement (layer 4): requests, budgets, sampling, scheduler and join candidates (spec 8).

Numbers come only from a :class:`MeasureBackend`; the real one is
``schemaguru.measure.flbridge.FactLangBackend`` (imported from its module: it is the FactLang
seam). SchemaGuru never writes measurement SQL.
"""

from schemaguru.measure.backend import CostEstimate, MeasureBackend, MeasureResult
from schemaguru.measure.budget import BudgetGuard
from schemaguru.measure.candidates import (
    JoinCandidateSource,
    join_candidate_sources,
    join_candidates,
    register_join_candidate_source,
)
from schemaguru.measure.catalog_bridge import StoreCatalog
from schemaguru.measure.convert import to_facts
from schemaguru.measure.estimates import EstimatedValue, scale_count, wilson
from schemaguru.measure.requests import (
    Freshness,
    KeyOverlapReq,
    PatternRate,
    ProfileColumns,
    Request,
    RowCount,
    SampleChoice,
    TopValues,
    Uniqueness,
)
from schemaguru.measure.sampling import choose_sample, is_large, seed_for
from schemaguru.measure.scheduler import BackgroundDrainer, MeasureReport, Scheduler

__all__ = [
    "BackgroundDrainer",
    "BudgetGuard",
    "CostEstimate",
    "EstimatedValue",
    "Freshness",
    "JoinCandidateSource",
    "KeyOverlapReq",
    "MeasureBackend",
    "MeasureReport",
    "MeasureResult",
    "PatternRate",
    "ProfileColumns",
    "Request",
    "RowCount",
    "SampleChoice",
    "Scheduler",
    "StoreCatalog",
    "TopValues",
    "Uniqueness",
    "choose_sample",
    "is_large",
    "join_candidate_sources",
    "join_candidates",
    "register_join_candidate_source",
    "scale_count",
    "seed_for",
    "to_facts",
    "wilson",
]
