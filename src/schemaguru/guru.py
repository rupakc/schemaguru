"""The ``Guru`` façade (layer 7; spec 2 G1/G2/G9, 8.2, 12, 18, 19): one object over everything.

``Guru.connect(url)`` opens a warehouse through the FactLang seam, crawls an empty store and
answers ``guru.context(question)`` with a question-scoped view. Every subsystem stays usable on
its own; the façade only wires them:

* **No spend on the query path** (spec 8.2, 25.10). ``context()`` reads the store; tables it
  shows that were never measured are pushed on the measurement queue, and nothing else is
  written. The queue is drained by ``measure()`` / ``measure_queue()`` / ``refresh()``, by a
  daemon thread when ``Config(measure_on_demand="background")``, or synchronously for the
  scoped tables when ``measure_on_demand="block"`` (the user's explicit opt-in to spend; capped
  by ``Config.block_timeout_s`` and run outside the façade lock).
* **One scheduler** per Guru, so one measurement run at a time: budget and exact-sampling
  overrides are run-level arguments, and a config change stops the drainer (waiting for it),
  reconfigures the scheduler between runs and restarts the drainer only in background mode.
* **View cache** (spec 12.2): an LRU of ``Config.scope.cache_entries`` views keyed on the store's
  latest snapshot id, the question hash, the config hash, profile and budget, plus a write
  generation. Every write through the façade (crawl, measure, draft, accept, overlays,
  refresh, verify; the background drainer's runs) bumps the generation, so a cached view never
  outlives the knowledge it rendered. Writes made by *another* process to a shared store are
  seen at the next snapshot or façade write.
* **Graph**: built from the store's JOIN/ENTITY_TYPE facts once per (generation, snapshot, day)
  and shared by scoping, rendering and ``find_path``. Its per-config memo is not thread-safe, so
  every use of it (view rendering on a cache miss, path search) runs under one re-entrant lock:
  concurrent ``context()`` calls are safe and serialise on a miss.
* **Overlays**: filters are re-checked with the render seam's ``validate_filter`` (FactLang's
  parser when installed); waypoint rules are validated and linted at load, and kept *raw* so
  each search compiles them itself and a malformed rule still refuses the searches it governs
  (fail closed). Chains compile to pair rules against the current graph. Every diagnostic lands
  in :attr:`Guru.diagnostics`. Each ``load_overlays`` call replaces the previous rule set, so
  pass every overlay path in one call; HUMAN facts are upserted into the store.
* **Names**: a bare table name resolves through ``store.resolve`` and then by unqualified name;
  an ambiguous name raises :class:`SchemaGuruError` listing the candidates (never a guess).
"""

from __future__ import annotations

import asyncio
import fnmatch
import math
import os
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar, overload

from schemaguru import plugins
from schemaguru._util.hashing import stable_hash
from schemaguru._util.redact import scrub_text
from schemaguru._util.timeutil import require_aware, utcnow
from schemaguru.catalog.crawl import CrawlReport
from schemaguru.catalog.crawl import crawl as _crawl
from schemaguru.catalog.executor import Connection, FactLangExecutor, as_connector
from schemaguru.catalog.executor import connect as _connect
from schemaguru.catalog.protocol import CatalogError, CatalogReader
from schemaguru.catalog.registry import reader_for
from schemaguru.config import Budget, Config
from schemaguru.diagnostics import Diagnostic, DiagnosticBag, Severity
from schemaguru.draft.accept import accept as _accept
from schemaguru.draft.accept import write_accepted
from schemaguru.draft.pipeline import DraftReport, draft_tables
from schemaguru.draft.provider import LLMProvider
from schemaguru.draft.verify import verify_drafts
from schemaguru.errors import AmbiguousNameError, SchemaGuruError
from schemaguru.feedback.intake import report_error as _report_error
from schemaguru.graph.chains import compile_chain
from schemaguru.graph.edges import build_edges, entity_tables
from schemaguru.graph.hubs import Hubs
from schemaguru.graph.search import Automaton, Graph, PathResult, find_paths
from schemaguru.graph.waypoints import compile_rules, lint_rules, validate_rules
from schemaguru.knowledge.resolve import resolve
from schemaguru.knowledge.soft_delete import store_mismatch
from schemaguru.measure.backend import CostEstimate, MeasureBackend, MeasureResult
from schemaguru.measure.budget import BudgetGuard
from schemaguru.measure.catalog_bridge import StoreCatalog
from schemaguru.measure.flbridge import FactLangBackend
from schemaguru.measure.requests import Request
from schemaguru.measure.scheduler import BackgroundDrainer, MeasureReport, Scheduler
from schemaguru.model.chains import Chain, FilterStep
from schemaguru.model.evidence import Evidence
from schemaguru.model.facts import Fact, Provenance, Status, Waiver
from schemaguru.model.refs import ObjectRef
from schemaguru.overlays.dbt import apply_dbt
from schemaguru.overlays.loader import load_overlays as _load_overlays
from schemaguru.overlays.loader import retract_removed
from schemaguru.overlays.schema import WaypointSpec
from schemaguru.pii.policy import lint_per_glob
from schemaguru.refresh.report import RefreshReport
from schemaguru.refresh.run import refresh as _refresh
from schemaguru.render.fingerprint import fingerprint_views as _fingerprint_views
from schemaguru.render.formats import format_view
from schemaguru.render.fragments import validate_filter
from schemaguru.render.view import ContextView, ViewRenderer
from schemaguru.scope.expand import expand
from schemaguru.scope.pipeline import NOT_SHOWN_PROBE, build_scope
from schemaguru.scope.retrievers import LexicalRetriever, Retriever
from schemaguru.scope.tokens import CharTokenCounter, TiktokenCounter, TokenCounter
from schemaguru.store.export import export_json
from schemaguru.store.open import open_store
from schemaguru.store.protocol import Store

Target = str | ObjectRef
_T = TypeVar("_T")
FIRST_TOUCH = "first_touch"  # queue reason for a table a question scoped before it was measured
_GLOB = frozenset("*?[")


class _NoBackend:
    """The backend of a Guru without a connector: every probe reports ``SG304``."""

    def capabilities(self) -> frozenset[str]:
        return frozenset()

    def measure(self, reqs: Sequence[Request]) -> list[MeasureResult]:
        d = Diagnostic("SG304", Severity.ERROR, "no measurement backend (no connector)")
        return [MeasureResult(r, None, None, d) for r in reqs]

    def estimate(self, reqs: Sequence[Request]) -> list[CostEstimate]:
        return [CostEstimate(r, None, "unknown") for r in reqs]

    def check(self, claims: str) -> list[Evidence]:
        return []


class _Scheduler(Scheduler):
    """A scheduler that tells the façade after every run (its writes invalidate cached views)."""

    def __init__(
        self,
        store: Store,
        backend: MeasureBackend,
        config: Config,
        *,
        now: Callable[[], datetime],
        on_write: Callable[[], None],
    ) -> None:
        super().__init__(store, backend, config, now=now)
        self._on_write = on_write

    def run(
        self,
        refs: Sequence[ObjectRef] | None = None,
        *,
        drain_queue: bool = False,
        guard: BudgetGuard | None = None,
        config: Config | None = None,
    ) -> MeasureReport:
        try:
            return super().run(refs, drain_queue=drain_queue, guard=guard, config=config)
        finally:
            self._on_write()


class _LRU:
    def __init__(self, capacity: int) -> None:
        self.capacity = max(0, capacity)
        self._items: OrderedDict[tuple[Any, ...], ContextView] = OrderedDict()

    def get(self, key: tuple[Any, ...]) -> ContextView | None:
        hit = self._items.get(key)
        if hit is not None:
            self._items.move_to_end(key)
        return hit

    def put(self, key: tuple[Any, ...], value: ContextView) -> None:
        if self.capacity == 0:
            return
        self._items[key] = value
        self._items.move_to_end(key)
        while len(self._items) > self.capacity:
            self._items.popitem(last=False)

    def clear(self) -> None:
        self._items.clear()


def _counter_for(cfg: Config) -> TokenCounter:
    return TiktokenCounter() if cfg.token_counter == "tiktoken" else CharTokenCounter()


class Guru:
    """Schema intelligence for one warehouse: crawl, measure, draft, overlay, scope, render."""

    def __init__(
        self,
        *,
        connector: Any = None,
        reader: CatalogReader | None = None,
        store: Store | str | os.PathLike[str] | None = None,
        backend: MeasureBackend | None = None,
        config: Config | None = None,
        name: str = "default",
        dialect: str | None = None,
        retriever: Retriever | None = None,
        token_counter: TokenCounter | None = None,
        now: Callable[[], datetime] = utcnow,
        drain_interval_s: float = 5.0,
    ) -> None:
        self.name = name
        self._config = config or Config()
        self._now = now
        self._lock = threading.RLock()
        self._diags: dict[tuple[str, str, str | None], Diagnostic] = {}
        if connector is not None:  # a DB-API connection, engine, client or callable (G2)
            connector = as_connector(connector, dialect)
        self.dialect: str | None = (
            dialect or getattr(connector, "dialect", None) or getattr(reader, "dialect", None)
        )
        self._owns_store = store is None or isinstance(store, str | os.PathLike)
        if store is None or isinstance(store, str | os.PathLike):
            path = store or self._config.store_path or os.path.join(".schemaguru", f"{name}.db")
            # a postgres:// DSN opens PostgresStore; another URL is a ConfigError, never a file
            self.store: Store = open_store(path)
        else:
            self.store = store
        self.connector = connector
        if reader is None and connector is not None and isinstance(self.dialect, str):
            try:
                reader = reader_for(self.dialect, FactLangExecutor(connector))
            except CatalogError as e:  # crawl() raises later; the cause is kept here
                self._note(Diagnostic(e.code, Severity.ERROR, str(e)))
        self.reader = reader
        self._auto_backend = backend is None
        self.backend: MeasureBackend = backend or self._default_backend()
        self.retriever: Retriever = retriever or LexicalRetriever(self.store)
        self._explicit_counter = token_counter is not None
        self.counter: TokenCounter = token_counter or _counter_for(self._config)
        self.drainer: BackgroundDrainer | None = None
        self._drain_interval_s = drain_interval_s
        self._connection: Connection | None = None
        self._generation = 0
        self._cache = _LRU(self._config.scope.cache_entries)
        self._graph_key: tuple[Any, ...] | None = None
        self._graph: Graph | None = None
        self._rule_specs: list[WaypointSpec] = []  # raw overlay rules + compiled chains
        self._waypoints: list[WaypointSpec] = []
        self._chains: list[Chain] = []
        self._waivers: tuple[Waiver, ...] = ()
        self._scheduler = self._make_scheduler(self._config)
        # config switches: serialised; while one runs no drainer may start (see the setter)
        self._config_lock = threading.Lock()
        self._switching = False
        # in-flight block-mode runs; close() waits for them (they use the store)
        self._runs = threading.Condition()
        self._active_runs = 0
        self._closing = False
        # the one per-period budget guard for spend nobody asked for explicitly: the background
        # drainer and agent claim checks share it (one total, refilled per Budget.period_s)
        self._period: BudgetGuard | None = None

    # ----- construction ---------------------------------------------------------------------

    @classmethod
    def connect(
        cls,
        url: str,
        *,
        name: str = "default",
        store: Store | str | os.PathLike[str] | None = None,
        config: Config | None = None,
        backend: MeasureBackend | None = None,
        crawl: bool | None = None,
    ) -> Guru:
        """Open ``url`` through the FactLang seam (built-in DuckDB fallback) and build a Guru.

        The store defaults to ``./.schemaguru/<name>.db``. ``crawl=None`` crawls the catalog
        (metadata only) when the store holds no table yet, so the three-line quickstart works;
        ``True`` always crawls, ``False`` never.
        """
        conn = _connect(url)
        try:
            reader = reader_for(conn.dialect, conn.executor)
            guru = cls(
                connector=conn.connector,
                reader=reader,
                store=store,
                backend=backend,
                config=config,
                name=name,
                dialect=conn.dialect,
            )
        except BaseException:
            conn.close()
            raise
        guru._connection = conn
        empty = next(iter(guru.store.iter_tables()), None) is None
        if crawl or (crawl is None and empty):
            guru.crawl()
        return guru

    def __repr__(self) -> str:
        shown = None if self._connection is None else self._connection.shown_url
        where = f" url={shown!r}" if shown else ""
        return f"<Guru name={self.name!r} dialect={self.dialect!r}{where}>"

    def close(self) -> None:
        """Stop the drainer and wait for in-flight runs, then close what this Guru opened."""
        with self._runs:
            self._closing = True
        self._stop_drainer()
        with self._runs:
            while self._active_runs:
                self._runs.wait()
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        if self._owns_store:
            close = getattr(self.store, "close", None)
            if callable(close):
                close()

    def __enter__(self) -> Guru:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def config(self) -> Config:
        return self._config

    @config.setter
    def config(self, value: Config) -> None:
        """Swap the config: the drainer is stopped (and restarted only in background mode).

        The one scheduler is reconfigured between runs; nothing here holds the façade lock while
        waiting for a run, because a finishing run takes that lock to invalidate views.
        """
        with self._config_lock:
            with self._lock:  # from here until the swap, no drainer can start
                self._switching = True
            try:
                was_draining = self._stop_drainer()
                backend = self._default_backend(value) if self._auto_backend else None
                self._scheduler.reconfigure(value, backend)
                with self._lock:
                    self._config = value
                    if backend is not None:  # the FactLang backend scales samples by the config
                        self.backend = backend
                    self._cache = _LRU(value.scope.cache_entries)
                    if not self._explicit_counter:
                        self.counter = _counter_for(value)
                    if self._period is not None:
                        self._period.rebudget(value.budget)  # keeps this period's spend
                    self._invalidate()
            finally:
                with self._lock:
                    self._switching = False
            if value.measure_on_demand != "background":
                self._stop_drainer()  # belt and braces: nothing may outlive a switch away
            elif was_draining:
                self._ensure_drainer()

    def _stop_drainer(self) -> bool:
        """Stop the drainer and WAIT for it (never under the façade lock); True if it ran."""
        with self._lock:
            drainer, self.drainer = self.drainer, None
        if drainer is None:
            return False
        alive = drainer.is_alive()
        drainer.stop(timeout=None)
        return alive

    def _default_backend(self, cfg: Config | None = None) -> MeasureBackend:
        if self.connector is None:
            return _NoBackend()
        sampling = (cfg or self._config).sampling
        return FactLangBackend(
            self.connector,
            catalog=StoreCatalog(self.store),
            sampling=sampling,
            dialect=self.dialect,
        )

    def _make_scheduler(self, cfg: Config) -> _Scheduler:
        return _Scheduler(self.store, self.backend, cfg, now=self._now, on_write=self._invalidate)

    # ----- diagnostics and invalidation -----------------------------------------------------

    @property
    def diagnostics(self) -> tuple[Diagnostic, ...]:
        """Every distinct diagnostic the façade has seen (overlays, rules, chains, wiring)."""
        with self._lock:
            mine = tuple(self._diags.values())
        return mine + tuple(d for d in plugins.diagnostics() if d not in mine)

    def _note(self, d: Diagnostic) -> None:
        with self._lock:
            self._diags.setdefault((d.code, d.message, d.location), d)

    def _invalidate(self) -> None:
        with self._lock:
            self._generation += 1
            self._cache.clear()

    # ----- names ------------------------------------------------------------------------------

    def table_ref(self, name: Target) -> ObjectRef:
        """The stored table ``name`` refers to (a bare name works when it is unambiguous)."""
        if isinstance(name, ObjectRef):
            if self.store.table(name.table_ref()) is not None:
                return name.table_ref()
            name = name.table_ref().canonical()
        ref, cands = self.store.resolve(name)
        if ref is not None:
            return ref.table_ref()
        if not cands:
            try:
                parsed = ObjectRef.parse(name)
            except ValueError:
                raise SchemaGuruError(f"unknown table {name!r}") from None
            if parsed.schema is None and parsed.catalog is None:
                cands = self.store.tables_named([parsed.table], limit=50)
                if len(cands) == 1:
                    return cands[0]
        if cands:
            listed = ", ".join(c.canonical() for c in cands)
            raise AmbiguousNameError(
                f"table name {name!r} is ambiguous: {listed}",
                tuple(c.canonical() for c in cands),
            )
        raise SchemaGuruError(f"unknown table {name!r} (crawl first, or qualify the name)")

    def column_ref(self, name: Target) -> ObjectRef:
        """The stored column ``name`` (``table.column``, the table part resolved as a table)."""
        if isinstance(name, ObjectRef):
            ref = name
        else:
            try:
                ref = ObjectRef.parse(name, has_column=True)
            except ValueError:
                raise SchemaGuruError(f"not a column reference: {name!r}") from None
        if ref.column is None:
            raise SchemaGuruError(f"not a column reference: {name!r}")
        table = self.table_ref(ref.table_ref())
        for col in self.store.columns(table):
            if (col.ref.column or "").casefold() == ref.column.casefold():
                return col.ref
        raise SchemaGuruError(f"unknown column {ref.column!r} in {table.canonical()}")

    def _targets(self, targets: Target | Iterable[Target]) -> list[ObjectRef]:
        items: list[Target] = [targets] if isinstance(targets, str | ObjectRef) else list(targets)
        out: list[ObjectRef] = []
        for item in items:
            if isinstance(item, str) and _GLOB & set(item):
                hits = [
                    t.ref
                    for t in self.store.iter_tables()
                    if fnmatch.fnmatchcase(t.ref.canonical(), item)
                    or fnmatch.fnmatchcase(t.ref.canonical().casefold(), item.casefold())
                ]
                if not hits:
                    raise SchemaGuruError(f"pattern {item!r} matches no table")
                out += hits
            else:
                out.append(self.table_ref(item))
        return list(dict.fromkeys(out))

    # ----- discovery and measurement ----------------------------------------------------------

    def _need_reader(self) -> CatalogReader:
        if self.reader is None:
            raise SchemaGuruError(
                "this Guru has no catalog reader: pass reader= (or a connector with a dialect)"
            )
        return self.reader

    def crawl(
        self, *, include: Sequence[str] | None = None, exclude: Sequence[str] | None = None
    ) -> CrawlReport:
        """Index the catalog (metadata only); ``include``/``exclude`` override the config."""
        reader = self._need_reader()
        cfg = self._config.crawl
        if include is not None:
            cfg = replace(cfg, include=tuple(include))
        if exclude is not None:
            cfg = replace(cfg, exclude=tuple(exclude))
        try:
            return _crawl(
                reader,
                self.store,
                config=cfg,
                now=self._now,
                crawl_bytes=self._config.budget.crawl_bytes,
            )
        finally:
            self._invalidate()

    def _no_measure(self) -> MeasureReport | None:
        if "measure" in self.backend.capabilities():
            return None
        report = MeasureReport()
        report.errors.append(
            Diagnostic(
                "SG304",
                Severity.ERROR,
                "the measurement backend cannot measure (the installed FactLang lacks "
                "Checker.measure, or no connector was given); nothing was measured",
                hint="pass backend=..., or upgrade FactLang",
            )
        )
        return report

    def measure(
        self,
        targets: Target | Iterable[Target] | None = None,
        *,
        all: bool = False,
        budget: Budget | None = None,
    ) -> MeasureReport:
        """Measure ``targets`` (names or globs); with none, drain the queue (spec 8.2).

        ``all=True`` measures every live table, hottest first (feedback, declared foreign keys,
        log row count). ``budget`` replaces the configured one for this run.
        """
        blocked = self._no_measure()
        if blocked is not None:
            return blocked
        # one scheduler per Guru (one lock): a budget override is a run-level guard
        guard = None if budget is None else BudgetGuard(budget, self.store)
        sched = self._scheduler
        if all:
            return sched.run(self._eager_order(), guard=guard)
        if targets is None:
            return sched.run(drain_queue=True, guard=guard)
        return sched.run(self._targets(targets), guard=guard)

    def measure_queue(self) -> MeasureReport:
        """Drain the measurement queue (first-touch and feedback tables) under the budget."""
        return self.measure()

    def _eager_order(self) -> list[ObjectRef]:
        heat: dict[str, int] = {}
        for row in self.store.feedback_summary():
            key = str(row["table"])
            count = row["count"]
            heat[key] = heat.get(key, 0) + (count if isinstance(count, int) else 0)
        scored: list[tuple[float, str, ObjectRef]] = []
        for t in self.store.iter_tables():
            fks = sum(1 for c in self.store.constraints(t.ref) if c.kind == "foreign_key")
            rows = math.log1p(t.row_count) if t.row_count else 0.0
            score = heat.get(t.ref.canonical(), 0) + fks + rows
            scored.append((-score, t.ref.canonical(), t.ref))
        return [ref for _, _, ref in sorted(scored)]

    def refresh(self, *, label: str | None = None) -> RefreshReport:
        """Incremental crawl, re-measure stale/changed tables, snapshot and classify (spec 15)."""
        reader = self._need_reader()
        try:
            return _refresh(
                self.store,
                reader,
                self._scheduler,
                config=self._config,
                now=self._now,
                label=label,
            )
        finally:
            self._invalidate()

    # ----- drafting and verification ----------------------------------------------------------

    def draft(
        self,
        targets: Target | Iterable[Target],
        *,
        provider: LLMProvider,
        budget: Budget | int | None = None,
        verify: bool = True,
    ) -> DraftReport:
        """Draft table meaning with an LLM (never on the query path); verify checkable parts."""
        refs = self._targets(targets)
        try:
            report = draft_tables(
                refs, store=self.store, provider=provider, config=self._config, budget=budget
            )
            if verify and report.drafted:
                bag = DiagnosticBag()
                checked = verify_drafts(
                    report.drafted,
                    self.backend,
                    config=self._config,
                    diagnostics=bag,
                    now=self._now(),
                    store=self.store,
                )
                self.store.put_facts(checked)
                report.drafted = checked
                report.diagnostics += list(bag)
            return report
        finally:
            self._invalidate()

    def accept(
        self, ids: Iterable[str], *, write_overlay: str | os.PathLike[str] | None = None
    ) -> list[Fact]:
        """Turn drafted facts into HUMAN facts; optionally append them to an accepted overlay."""
        try:
            accepted = _accept(self.store, ids, now=self._now())
            if write_overlay is not None:
                write_accepted(accepted, Path(write_overlay))
            return accepted
        finally:
            self._invalidate()

    @overload
    def verify(self, facts_or_claims: str, exact: bool = False) -> list[Evidence]: ...

    @overload
    def verify(self, facts_or_claims: Sequence[Fact], exact: bool = False) -> list[Fact]: ...

    def verify(
        self, facts_or_claims: str | Sequence[Fact], exact: bool = False
    ) -> list[Evidence] | list[Fact]:
        """Check FactLang claim text, or facts: drafts become claims (spec 10.3).

        Claims are never sampled. With ``exact=True`` the tables behind sampled (ESTIMATED)
        measurements are re-measured exactly within budget, and the exact facts are appended
        (a sample can refute but never confirm, spec 8.6).
        """
        if isinstance(facts_or_claims, str):
            return list(self.backend.check(facts_or_claims))
        facts = list(facts_or_claims)
        bag = DiagnosticBag()
        try:
            out = verify_drafts(
                facts,
                self.backend,
                config=self._config,
                diagnostics=bag,
                now=self._now(),
                store=self.store,
            )
            changed = [g for f, g in zip(facts, out, strict=True) if f != g]
            if changed:
                self.store.put_facts(changed)
            for d in bag:
                self._note(d)
            if exact:
                out += self._exact(facts)
            return out
        finally:
            self._invalidate()

    def _exact(self, facts: Sequence[Fact]) -> list[Fact]:
        sampled = [
            f for f in facts if f.provenance is Provenance.MEASURED and f.status is Status.ESTIMATED
        ]
        if not sampled or self._no_measure() is not None:
            return []
        tables: list[ObjectRef] = []
        for f in sampled:
            s = f.subject
            if isinstance(s, ObjectRef):
                tables.append(s.table_ref())
            else:
                tables += [x.table_ref() for x in (*s.left, *s.right)]
        exact_cfg = replace(
            self._config, sampling=replace(self._config.sampling, measurement="off")
        )
        self._scheduler.run(list(dict.fromkeys(tables)), config=exact_cfg)
        wanted = {(f.kind.name, f.subject) for f in sampled}
        fresh = self.store.facts_for(
            list({f.subject for f in sampled}), sorted({f.kind.name for f in sampled})
        )
        return [
            f
            for f in fresh
            if (f.kind.name, f.subject) in wanted
            and f.provenance is Provenance.MEASURED
            and f.method == "exact"
        ]

    # ----- overlays ---------------------------------------------------------------------------

    def load_overlays(self, *paths: str | os.PathLike[str]) -> list[Diagnostic]:
        """Load human overlay files/directories (spec 14); returns this load's diagnostics.

        Never raises on data. Replaces the previously loaded rules, chains, waivers and overlay
        facts: an entry removed from its file (or a file no longer passed) has its facts
        retracted, except for a file that failed to load, which keeps them until it loads again.
        A human soft-delete filter that does not fit the catalog or the measured values is
        reported (``SG210``) and shown with a warning, never applied silently.
        """
        today = require_aware(self._now()).date()
        facts, chains, waypoints, waivers, bag = _load_overlays(
            [Path(p) for p in paths], self.store, today=today
        )
        dialect = self.dialect or "ansi"
        out: list[Diagnostic] = list(bag)

        def filter_ok(text: str, where: str | None) -> bool:
            ok = True
            for d in validate_filter(text, dialect):
                if d.severity is Severity.ERROR:
                    ok = False
                    out.append(replace(d, location=d.location or where))
                elif not any(o.code == d.code for o in out):
                    out.append(d)
            return ok

        kept: list[Fact] = []
        for f in facts:
            pred = f.value.get("predicate") if f.kind.name == "SOFT_DELETE" else None
            if isinstance(pred, str) and not filter_ok(pred, f.source):
                continue
            kept.append(f)
        good_chains: list[Chain] = []
        for chain in chains:
            steps_ok = [
                filter_ok(s.filter_text, f"chain:{chain.name}")
                for s in chain.steps
                if isinstance(s, FilterStep)
            ]
            if all(steps_ok):
                good_chains.append(chain)
        out += validate_rules(waypoints)
        with self._lock:
            if kept:
                self.store.put_facts(kept)
            # entries removed since the last load take their facts with them (17b)
            retract_removed(self.store, kept, bag.failed_files)
            now = require_aware(self._now())
            for f in kept:  # a declared live-rows filter is checked, never applied blind (6d)
                why = store_mismatch(self.store, f)
                if why is None or not isinstance(f.subject, ObjectRef):
                    continue
                key = ("SOFT_DELETE", f.subject.table_ref().canonical())
                if any(w.active(now) and w.matches(*key) for w in waivers):
                    continue  # waived, as in the view (render/knowledge.py)
                out.append(
                    Diagnostic(
                        "SG210",
                        Severity.WARNING,
                        f"the soft-delete filter of {key[1]} does not match the data: {why}; it "
                        "is shown with a warning and left out of SQL snippets",
                        f.source,
                    )
                )
            self._waypoints = list(waypoints)
            self._chains = good_chains
            self._waivers = tuple(waivers)
            self._invalidate()
            graph = self._current_graph(out)
            out += lint_rules(self._waypoints, graph.edges)
            # a per_glob typo silently leaves its tables on the default mode (spec 11)
            out += lint_per_glob(self._config.sampling, (t.ref for t in self.store.iter_tables()))
            for d in out:
                self._note(d)
        return out

    def import_dbt(self, manifest_path: str | os.PathLike[str]) -> list[Diagnostic]:
        """Import a dbt manifest (descriptions, tests, table and column lineage; spec 4.2) and
        return this import's diagnostics. Lineage the new manifest no longer produces is
        retracted. Never raises on data; an unreadable manifest (``SG209``) changes nothing."""
        bag = DiagnosticBag()
        with self._lock:
            apply_dbt(manifest_path, self.store, diagnostics=bag)
            self._invalidate()
            out = list(bag)
            for d in out:
                self._note(d)
        return out

    # ----- graph and paths --------------------------------------------------------------------

    def _current_graph(self, sink: list[Diagnostic] | None = None) -> Graph:
        """The join graph for the current knowledge (caller holds ``self._lock``)."""
        now = require_aware(self._now())
        key = (self._generation, self.store.latest_snapshot(), now.date(), self._config.graph)
        if self._graph is not None and self._graph_key == key:
            return self._graph
        cfg = self._config
        facts = self.store.facts_for(None, ["JOIN", "ENTITY_TYPE"])
        eff = resolve(facts, now=now, ttl_days=cfg.fact_ttl_days, waivers=self._waivers)
        bag = DiagnosticBag()
        edges = build_edges(eff, cfg=cfg.graph, bag=bag)
        graph = Graph(edges, Hubs.from_edges(edges, entity_tables(eff), cfg.graph))
        specs = list(self._waypoints)
        for chain in self._chains:
            spec = compile_chain(chain, graph, bag=bag)
            if spec is not None:
                specs.append(spec)
        self._rule_specs = specs
        for d in bag:
            self._note(d)
            if sink is not None:
                sink.append(d)
        self._graph, self._graph_key = graph, key
        return graph

    def _rules(self, src: ObjectRef, dst: ObjectRef) -> Automaton | None:
        if not self._rule_specs:
            return None
        return compile_rules(self._rule_specs, src, dst)

    def find_path(self, a: Target, b: Target, *, k: int | None = None) -> PathResult:
        """Measured join routes between two tables, or a typed refusal (spec 9)."""
        src, dst = self.table_ref(a), self.table_ref(b)
        with self._lock:
            graph = self._current_graph()
            cfg = self._config.graph if k is None else replace(self._config.graph, k_paths=k)
            return find_paths(graph, src, dst, cfg=cfg, automaton=self._rules(src, dst))

    # ----- context ----------------------------------------------------------------------------

    def _renderer(self, profile: str) -> ViewRenderer:
        return ViewRenderer(
            self.store,
            now=self._now(),
            cfg=self._config,
            profile=profile,
            graph=self._current_graph(),
            waivers=self._waivers,
            dialect=self.dialect,
            rules=self._rules,
        )

    def context(
        self,
        question: str,
        *,
        budget_tokens: int = 4000,
        profile: str = "agent",
        format: str = "text",
    ) -> ContextView:
        """The question-scoped view an agent reads (spec 12, 13); ``str(view)`` is ``format``.

        Read-only apart from the measurement queue: shown tables that were never measured are
        enqueued. It spends warehouse money only when ``Config.measure_on_demand`` says so.
        """
        try:
            format_view(ContextView("", (), (), "", 0), format)
        except KeyError as e:
            raise ValueError(f"unknown format {format!r}: {e}") from None
        cfg = self._config
        with self._lock:
            key = (
                self._generation,
                self.store.latest_snapshot(),
                stable_hash(question),
                stable_hash(repr(cfg)),
                profile,
                budget_tokens,
                require_aware(self._now()).date(),
            )
            view = self._cache.get(key)
            block: tuple[ObjectRef, ...] = ()
            if view is None:
                view, unmeasured = self._render(question, budget_tokens, profile)
                if (
                    unmeasured
                    and cfg.measure_on_demand == "block"
                    and self._no_measure() is None
                    and self._begin_run()
                ):
                    block = unmeasured
                else:
                    self._enqueue(unmeasured)
                    self._cache.put(key, view)
        if block:
            # block mode (the user's opt-in to spend): measure OUTSIDE the façade lock, through
            # the one per-period guard the drainer and check_fact share (so N calls cannot
            # spend N totals), overriding only the seconds: block_timeout_s, started here,
            # before the scheduler lock is taken. Whatever the cap leaves is re-queued.
            cap = cfg.budget.max_seconds
            seconds = cfg.block_timeout_s if cap is None else min(cap, cfg.block_timeout_s)
            guard = self._period_guard()
            try:
                with guard.deadline(seconds):
                    self._scheduler.run(list(block), guard=guard)
                with self._lock:
                    view, _ = self._render(question, budget_tokens, profile)
            finally:
                self._end_run()
        return view if format == "text" else replace(view, format=format)

    def _begin_run(self) -> bool:
        with self._runs:
            if self._closing:
                return False
            self._active_runs += 1
            return True

    def _end_run(self) -> None:
        with self._runs:
            self._active_runs -= 1
            self._runs.notify_all()

    def _render(
        self, question: str, budget_tokens: int, profile: str
    ) -> tuple[ContextView, tuple[ObjectRef, ...]]:
        r = self._renderer(profile)
        c = self.counter
        # the candidates the fitter may pick from (seeds plus bridges): their hub hazard lines
        # are reserved up front, because a view never drops them
        hits = self.retriever.search(question, self._config.scope.seed_k + NOT_SHOWN_PROBE)
        seeds = list(dict.fromkeys(ref.table_ref() for ref, _ in hits))[: self._config.scope.seed_k]
        bridges = (
            []
            if r.graph is None
            else expand(seeds, r.graph, self._config.scope, graph_cfg=self._config.graph)
        )
        scope = build_scope(
            question,
            retriever=self.retriever,
            store=self.store,
            graph=r.graph,
            renderer=r,
            counter=c,
            budget_tokens=r.table_budget(budget_tokens, c, tables=[*seeds, *bridges]),
            cfg=self._config.scope,
            graph_cfg=self._config.graph,
            hits=hits,
            bridges=bridges,
        )
        view = r.view(
            question,
            scope.tables,
            not_shown=scope.not_shown,
            budget_tokens=budget_tokens,
            counter=c,
        )
        return view, scope.unmeasured

    def _enqueue(self, unmeasured: Sequence[ObjectRef]) -> None:
        """First touch (spec 12.5): queue the tables; in background mode wake the drainer."""
        for ref in unmeasured:
            self.store.queue_push(ref, FIRST_TOUCH, 1.0)
        if unmeasured and self._config.measure_on_demand == "background":
            self._ensure_drainer()

    def _ensure_drainer(self) -> None:
        """Start the drainer only in background mode, never during a config switch or close."""
        with self._lock:
            if self._switching or self._closing:
                return
            if self._scheduler.config.measure_on_demand != "background":
                return
            if self.drainer is not None and self.drainer.is_alive():
                return
            if self._no_measure() is not None:
                return
            self.drainer = BackgroundDrainer(self._scheduler, self._drain_interval_s)
            self.drainer.guard = self._period_guard()
            self.drainer.on_error = self._drain_failed
            self.drainer.start()

    def _drain_failed(self, e: BaseException) -> None:
        """A background drain failed: one SG306 diagnostic per distinct error (the drainer
        retries with backoff, so the same error would otherwise repeat unseen)."""
        self._note(
            Diagnostic(
                "SG306",
                Severity.WARNING,
                f"background measurement failed and will be retried: {type(e).__name__}: "
                f"{scrub_text(str(e))}",
                hint="check the warehouse connection; the queue is kept",
            )
        )

    def fingerprint_views(self, questions: Iterable[str]) -> dict[str, str]:
        """``{question: fingerprint}`` for a host's CI to detect prompt drift (spec 13.4)."""
        return _fingerprint_views(self, questions)

    # ----- feedback, export -------------------------------------------------------------------

    def report_error(self, sql: str, error: str, dialect: str | None = None) -> int:
        """Record a failed agent query (identifiers and counts only); never raises (spec 16)."""
        try:
            return _report_error(self.store, sql, error, dialect or self.dialect)
        except Exception:
            return 0

    def feedback_summary(self) -> list[dict[str, object]]:
        return self.store.feedback_summary()

    def export(self, path: str | os.PathLike[str]) -> None:
        """Byte-deterministic JSON export of the store (spec 7.3)."""
        export_json(self.store, path)

    # ----- agent surface (spec 18; the logic lives in schemaguru.agent) -----------------------

    def tools(self) -> list[dict[str, Any]]:
        """JSON-schema tool specs (``name``, ``description``, ``input_schema``)."""
        from schemaguru.agent.tools import tool_definitions

        return tool_definitions(self)

    def handle_tool_call(self, name: str, args: Mapping[str, Any]) -> dict[str, Any]:
        """Run one tool; always JSON with a ``text`` field, never raises (errors: ``SG8xx``)."""
        from schemaguru.agent.dispatch import handle_tool_call

        return handle_tool_call(self, name, args)

    def prompt_block(self, question: str | None = None) -> str:
        """A system-prompt fragment: how to use the tools, the legend; with a question, its view."""
        from schemaguru.agent.prompt import prompt_block

        return prompt_block(self, question)

    def _with_renderer(self, fn: Callable[[ViewRenderer], _T], profile: str = "agent") -> _T:
        """Run ``fn`` on a renderer of the current knowledge, under the graph lock."""
        with self._lock:
            return fn(self._renderer(profile))

    def _period_guard(self) -> BudgetGuard:
        """The per-period guard shared by the background drainer and agent checks."""
        with self._lock:
            if self._period is None:
                self._period = BudgetGuard(self._config.budget, self.store)
            return self._period

    def _agent_guard(self) -> BudgetGuard:
        """The guard an agent's claim check is admitted through: the measurement period guard
        (spec 8.2, 8.3), never a separate allowance."""
        return self._period_guard()

    # ----- async twins (spec 19) --------------------------------------------------------------

    async def acrawl(
        self, *, include: Sequence[str] | None = None, exclude: Sequence[str] | None = None
    ) -> CrawlReport:
        return await asyncio.to_thread(self.crawl, include=include, exclude=exclude)

    async def ameasure(
        self,
        targets: Target | Iterable[Target] | None = None,
        *,
        all: bool = False,
        budget: Budget | None = None,
    ) -> MeasureReport:
        return await asyncio.to_thread(self.measure, targets, all=all, budget=budget)

    async def adraft(
        self,
        targets: Target | Iterable[Target],
        *,
        provider: LLMProvider,
        budget: Budget | int | None = None,
        verify: bool = True,
    ) -> DraftReport:
        return await asyncio.to_thread(
            self.draft, targets, provider=provider, budget=budget, verify=verify
        )

    async def acontext(
        self,
        question: str,
        *,
        budget_tokens: int = 4000,
        profile: str = "agent",
        format: str = "text",
    ) -> ContextView:
        return await asyncio.to_thread(
            self.context, question, budget_tokens=budget_tokens, profile=profile, format=format
        )

    async def arefresh(self, *, label: str | None = None) -> RefreshReport:
        return await asyncio.to_thread(self.refresh, label=label)

    async def afind_path(self, a: Target, b: Target, *, k: int | None = None) -> PathResult:
        return await asyncio.to_thread(self.find_path, a, b, k=k)


__all__ = ["FIRST_TOUCH", "Guru"]
