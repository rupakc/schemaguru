"""Resumable, paged, metadata-only crawl (spec 6.3).

Walks catalogs -> schemas -> table pages, fetches columns/constraints in batches on a bounded
worker pool, writes each page and its checkpoint in one store transaction, and soft-withdraws
tables that a complete listing no longer shows. Failures are isolated per page/batch and
reported, never raised; ``BaseException`` (e.g. ``KeyboardInterrupt``) propagates and leaves the
last checkpoint intact, so a later ``crawl(..., resume=True)`` continues from it.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from schemaguru._util.timeutil import to_iso, utcnow
from schemaguru.catalog.filters import ScopeFilter
from schemaguru.catalog.protocol import CatalogReader
from schemaguru.config import CrawlConfig
from schemaguru.diagnostics import Diagnostic, Severity
from schemaguru.model.refs import ObjectRef
from schemaguru.model.structure import Column, Constraint, Schema, Table
from schemaguru.store.protocol import Store

STATE_KEY = "crawl"
LAST_KEY = "crawl_last"


@dataclass(frozen=True)
class CrawlReport:
    tables_seen: int
    tables_written: int
    withdrawn: int
    errors: list[Diagnostic] = field(default_factory=list)
    queries: int = 0
    bytes_billed: int | None = None
    duration_s: float = 0.0


def _h(ref: ObjectRef) -> int:
    return int.from_bytes(hashlib.blake2b(ref.canonical().encode(), digest_size=8).digest(), "big")


def _parse_ts(value: object) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else None


def _chunks(items: Sequence[Table], n: int) -> list[Sequence[Table]]:
    return [items[i : i + n] for i in range(0, len(items), max(1, n))]


class _Crawl:
    def __init__(
        self,
        reader: CatalogReader,
        store: Store,
        config: CrawlConfig,
        resume: bool,
        incremental: bool,
        now: Callable[[], datetime],
    ) -> None:
        self.reader, self.store, self.cfg, self.now = reader, store, config, now
        self.filter = ScopeFilter(config.include, config.exclude)
        self.caps = reader.capabilities()
        self.errors: list[Diagnostic] = []
        self._qlock = threading.Lock()
        self.queries = 0
        self.tables_seen = 0
        self.tables_written = 0
        self.seen: set[int] = set()
        self.seen_schemas: set[tuple[str | None, str]] = set()
        self.complete_schemas: set[tuple[str | None, str]] = set()
        self.complete_catalogs: set[str | None] = set()
        self.listed_catalogs: list[str | None] = []
        self.catalogs_ok = True
        self.started = now()
        self.completed: list[list[str | None]] = []
        self.current: dict[str, Any] | None = None
        self.changed_since: datetime | None = None

        state = store.crawl_state_get(STATE_KEY) if resume else None
        if state is not None and state.get("status") == "running":
            done = state.get("completed_schemas")
            self.completed = [list(x) for x in done] if isinstance(done, list) else []
            cur = state.get("current")
            self.current = dict(cur) if isinstance(cur, dict) else None
            self.changed_since = _parse_ts(state.get("changed_since"))
            self.started = _parse_ts(state.get("started_at")) or self.started
        else:
            last = store.crawl_state_get(LAST_KEY)
            if incremental and self.caps.incremental and last is not None:
                self.changed_since = _parse_ts(last.get("started_at"))
        self._done = {(c, s) for c, s in self.completed}
        self._checkpoint()

    # -- bookkeeping -------------------------------------------------------------------------

    def _count(self, n: int = 1) -> None:
        with self._qlock:
            self.queries += n

    def _error(self, location: str, exc: BaseException) -> None:
        self.errors.append(
            Diagnostic(
                "SG102",
                Severity.ERROR,
                f"crawl skipped {location}: {type(exc).__name__}: {exc}",
                location=location,
            )
        )

    def _checkpoint(self) -> None:
        self.store.crawl_state_set(
            STATE_KEY,
            {
                "status": "running",
                "started_at": to_iso(self.started),
                "changed_since": None if self.changed_since is None else to_iso(self.changed_since),
                "completed_schemas": self.completed,
                "current": self.current,
            },
        )

    # -- listing -----------------------------------------------------------------------------

    def run(self, pool: ThreadPoolExecutor) -> int:
        catalogs: list[str | None] = []
        try:
            self._count()
            catalogs = list(self.reader.list_catalogs())
        except Exception as e:
            self._error("list_catalogs", e)
            self.catalogs_ok = False
        self.listed_catalogs = list(catalogs) if catalogs else [None]
        for catalog in self.listed_catalogs:
            self._crawl_catalog(catalog, pool)
        return self._withdraw() if self.catalogs_ok else 0

    def _crawl_catalog(self, catalog: str | None, pool: ThreadPoolExecutor) -> None:
        token: str | None = None
        while True:
            try:
                self._count()
                page = self.reader.list_schemas(catalog, page_token=token)
            except Exception as e:
                self._error(f"schemas of {catalog or 'default catalog'}", e)
                return
            for schema in page.items:
                self._crawl_schema(schema, pool)
            token = page.next_token
            if token is None:
                self.complete_catalogs.add(catalog)
                return

    def _crawl_schema(self, schema: Schema, pool: ThreadPoolExecutor) -> None:
        key = (schema.catalog, schema.name)
        if self.filter.excludes_schema(schema.catalog, schema.name):
            return
        self.seen_schemas.add(key)
        if key in self._done:
            return
        resume_token: str | None = None
        if (
            self.current is not None
            and (self.current.get("catalog"), self.current.get("schema")) == key
        ):
            resume_token = self.current.get("page_token")
        incremental = self.changed_since is not None
        self.store.upsert_schemas([schema])
        ok = self._walk(
            schema, resume_token, self.changed_since, pool, process=True, count_seen=not incremental
        )
        names_ok = True
        if ok and incremental:
            names_ok = self._walk(schema, None, None, pool, process=False, count_seen=True)
        if not ok:
            return
        self.completed.append([schema.catalog, schema.name])
        self._done.add(key)
        self.current = None
        self._checkpoint()
        if names_ok and (incremental or resume_token is None):
            self.complete_schemas.add(key)

    def _walk(
        self,
        schema: Schema,
        token: str | None,
        changed_since: datetime | None,
        pool: ThreadPoolExecutor,
        *,
        process: bool,
        count_seen: bool,
    ) -> bool:
        loc = f"{schema.key()}"
        while True:
            try:
                self._count()
                page = self.reader.list_tables(
                    schema, page_token=token, changed_since=changed_since
                )
            except Exception as e:
                self._error(loc, e)
                return False
            tables = [t for t in page.items if self.filter.allows(t.ref)]
            if count_seen:
                self.tables_seen += len(tables)
                self.seen.update(_h(t.ref) for t in tables)
            if process:
                with self.store.transaction():
                    self._write_page(tables, loc, pool)
                    self.current = {
                        "catalog": schema.catalog,
                        "schema": schema.name,
                        "page_token": page.next_token,
                    }
                    if page.next_token is not None:
                        self._checkpoint()
            token = page.next_token
            if token is None:
                return True

    # -- per-page writes ---------------------------------------------------------------------

    def _fetch(
        self, batch: Sequence[Table], loc: str
    ) -> tuple[dict[ObjectRef, list[Column]] | None, dict[ObjectRef, list[Constraint]] | None]:
        refs = [t.ref for t in batch]
        cols: dict[ObjectRef, list[Column]] | None = None
        cons: dict[ObjectRef, list[Constraint]] | None = None
        try:
            self._count()
            cols = dict(self.reader.columns(refs))
        except Exception as e:
            self._error(f"columns of a batch in {loc}", e)
        if self.caps.constraints:
            try:
                self._count()
                cons = dict(self.reader.constraints(refs))
            except Exception as e:
                self._error(f"constraints of a batch in {loc}", e)
        return cols, cons

    def _write_page(self, tables: list[Table], loc: str, pool: ThreadPoolExecutor) -> None:
        if not tables:
            return
        self.store.upsert_tables(tables)
        self.tables_written += len(tables)
        batches = _chunks(tables, self.cfg.column_batch)
        futures: list[Future[Any]] = [pool.submit(self._fetch, b, loc) for b in batches]
        for batch, fut in zip(batches, futures, strict=True):
            cols, cons = fut.result()
            for t in batch:
                if cols is not None:
                    self.store.upsert_columns(t.ref, cols.get(t.ref, []))
                if cons is not None:
                    self.store.upsert_constraints(t.ref, cons.get(t.ref, []))

    # -- withdrawal --------------------------------------------------------------------------

    def _withdraw(self) -> int:
        gone: list[ObjectRef] = []
        for t in self.store.iter_tables():
            ref = t.ref
            key = (ref.catalog, ref.schema or "")
            if key in self.complete_schemas:
                if _h(ref) not in self.seen:
                    gone.append(ref)
            elif key not in self.seen_schemas and (
                ref.catalog in self.complete_catalogs or ref.catalog not in self.listed_catalogs
            ):
                gone.append(ref)
        if gone:
            self.store.mark_withdrawn(gone, self.now())
        return len(gone)


def crawl(
    reader: CatalogReader,
    store: Store,
    *,
    config: CrawlConfig | None = None,
    resume: bool = True,
    incremental: bool = True,
    now: Callable[[], datetime] = utcnow,
) -> CrawlReport:
    """Crawl ``reader`` into ``store``.

    ``resume`` continues an interrupted crawl from its checkpoint (completed schemas are skipped,
    the in-progress one restarts at its saved page). ``incremental`` re-reads only tables the
    reader reports as changed since the last crawl that finished without errors, plus a name-only
    listing per schema to detect drops. A table is withdrawn only when a complete listing of its
    schema (from the first page, this run) omits it, or its schema/catalog is gone or now out of
    scope. If the reader has a ``page_size`` attribute it is set from ``config.page_size``.
    """
    cfg = config or CrawlConfig()
    t0 = time.monotonic()
    if isinstance(getattr(reader, "page_size", None), int):
        reader.page_size = cfg.page_size  # type: ignore[attr-defined]
    run = _Crawl(reader, store, cfg, resume, incremental, now)
    with ThreadPoolExecutor(max_workers=max(1, cfg.workers)) as pool:
        withdrawn = run.run(pool)
    if not run.errors:
        store.crawl_state_set(LAST_KEY, {"started_at": to_iso(run.started)})
    store.crawl_state_set(
        STATE_KEY,
        {
            "status": "complete",
            "started_at": to_iso(run.started),
            "finished_at": to_iso(now()),
            "tables_seen": run.tables_seen,
            "errors": len(run.errors),
        },
    )
    store.ledger_add("crawl", run.queries, "queries", "catalog crawl")
    bytes_billed = getattr(reader, "bytes_billed", None)
    if isinstance(bytes_billed, int):
        store.ledger_add("crawl", bytes_billed, "bytes", "catalog crawl")
    return CrawlReport(
        tables_seen=run.tables_seen,
        tables_written=run.tables_written,
        withdrawn=withdrawn,
        errors=run.errors,
        queries=run.queries,
        bytes_billed=bytes_billed if isinstance(bytes_billed, int) else None,
        duration_s=time.monotonic() - t0,
    )
