"""The scoping pipeline: retrieve, expand, fit (spec 12.1 steps 1-4, 12.3, 12.5)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from schemaguru.config import GraphConfig, ScopeConfig
from schemaguru.graph.search import Graph
from schemaguru.model.facts import Provenance
from schemaguru.model.joins import JoinRef
from schemaguru.model.refs import ObjectRef
from schemaguru.scope.budget import DetailLevel, TableRenderer, fit
from schemaguru.scope.expand import expand
from schemaguru.scope.retrievers import Retriever
from schemaguru.scope.tokens import TokenCounter
from schemaguru.store.protocol import Store

# Retrieval hits fetched beyond the seeds, to count the candidates that are "not shown".
NOT_SHOWN_PROBE = 100
# Table-subject kinds a measurement run writes; one of them MEASURED = "the table was measured".
MEASURED_TABLE_KINDS = ("ROW_COUNT", "FRESHNESS", "PRIMARY_KEY")


@dataclass(frozen=True)
class Scope:
    """The slice of the warehouse one question is answered from.

    ``tables`` in rank order (seeds by score, then bridges by path cost) with their detail
    level; ``not_shown`` counts the other candidate tables retrieval found (among the first
    ``seed_k + NOT_SHOWN_PROBE`` hits) plus scoped tables the budget dropped; ``unmeasured``
    are shown tables with no measured fact yet (the host enqueues them, spec 12.5).
    """

    question: str
    tables: tuple[tuple[ObjectRef, DetailLevel], ...]
    not_shown: int
    unmeasured: tuple[ObjectRef, ...]


def build_scope(
    question: str,
    *,
    retriever: Retriever,
    store: Store,
    graph: Graph | None,
    renderer: TableRenderer,
    counter: TokenCounter,
    budget_tokens: int,
    cfg: ScopeConfig,
    graph_cfg: GraphConfig | None = None,
    hits: Sequence[tuple[ObjectRef, float]] | None = None,
    bridges: Sequence[ObjectRef] | None = None,
) -> Scope:
    """Run the pipeline; read-only (no writes on the query path, spec 25.10).

    A caller that already ran ``retriever.search(question, cfg.seed_k + NOT_SHOWN_PROBE)``
    (e.g. to reserve budget for the candidates) passes its ``hits`` and the ``bridges``
    ``expand`` gave for their seeds, so retrieval and expansion run once.
    """
    if budget_tokens < 0:
        raise ValueError("budget_tokens must be >= 0")
    if hits is None:
        hits = retriever.search(question, cfg.seed_k + NOT_SHOWN_PROBE)
    candidates = list(dict.fromkeys(r.table_ref() for r, _ in hits))
    seeds = candidates[: cfg.seed_k]
    if bridges is None:
        bridges = [] if graph is None else expand(seeds, graph, cfg, graph_cfg=graph_cfg)
    ranked = list(dict.fromkeys([*seeds, *bridges]))
    chosen = fit(ranked, renderer, budget_tokens, counter)
    shown = {r for r, _ in chosen}
    not_shown = len({*candidates, *ranked} - shown)
    measured: set[ObjectRef] = set()
    subjects: list[ObjectRef | JoinRef] = list(shown)
    for f in store.facts_for(subjects, MEASURED_TABLE_KINDS):
        if f.provenance is Provenance.MEASURED and isinstance(f.subject, ObjectRef):
            measured.add(f.subject.table_ref())
    unmeasured = tuple(r for r, _ in chosen if r not in measured)
    return Scope(question, tuple(chosen), not_shown, unmeasured)
