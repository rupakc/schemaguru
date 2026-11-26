"""Resumable, paged, metadata-only crawl (spec 6.3).

Walks catalogs -> schemas -> table pages, fetches columns/constraints in batches on a bounded
worker pool, writes each page and its checkpoint in one store transaction, and soft-withdraws
tables that a complete listing no longer shows. Failures are isolated per page/batch and
reported, never raised; ``BaseException`` (e.g. ``KeyboardInterrupt``) propagates and leaves the
last checkpoint intact, so a later ``crawl(..., resume=True)`` continues from it.

Memory is bounded by the page size and the number of *schemas*, never by the number of tables:
the names seen this run live in a throw-away on-disk SQLite file, and the checkpoint is one small
cursor (the schema in progress and its page token), not a growing list.

After the walk, view definitions are parsed into lineage (0.2.0 spec 4.2-4.4) for the views
written this run and the live views without a stored definition hash. The raw definition text
lives in memory for one batch only: the store keeps the facts, the definition's sha256 and the
literal-free template; nothing logs or reports the text.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any, TypeVar

from schemaguru._util.timeutil import to_iso, utcnow
from schemaguru.catalog.filters import ScopeFilter
from schemaguru.catalog.protocol import (
    CatalogReader,
    ViewDefinitionReader,
    reads_view_definitions,
)
from schemaguru.config import CrawlConfig
from schemaguru.diagnostics import Diagnostic, Severity
from schemaguru.lineage.analyse import (
    DefinitionItem,
    DefinitionResult,
    analyse_definitions,
    definition_sha256,
    extra_missing_diagnostic,
    failure_diagnostic,
    lineage_facts,
)
from schemaguru.lineage.parse import lineage_available
from schemaguru.lineage.worker import ParserPool
from schemaguru.model.facts import Fact, Provenance
from schemaguru.model.kinds import LINEAGE_KINDS
from schemaguru.model.lineage import VIEW_SOURCE, RawName
from schemaguru.model.refs import ObjectRef
from schemaguru.model.structure import Column, Constraint, Schema, Table
from schemaguru.store.names import resolve_relation_names
from schemaguru.store.protocol import LineageIndex, Store, lineage_index

STATE_KEY = "crawl"
LAST_KEY = "crawl_last"
_NO_CATALOG = "\x00"
_VIEW_KINDS = frozenset({"view", "materialized_view"})
_T = TypeVar("_T")


@dataclass(frozen=True)
class CrawlReport:
    tables_seen: int
    tables_written: int
    withdrawn: int
    errors: list[Diagnostic] = field(default_factory=list)
    queries: int = 0
    bytes_billed: int | None = None
    bytes_billed_is_estimate: bool = False
    duration_s: float = 0.0
    views_parsed: int = 0
    lineage_s: float = 0.0
    lineage_diagnostics: list[Diagnostic] = field(default_factory=list)


class _OverCrawlBudget(Exception):
    """Raised between units of work once ``Budget.crawl_bytes`` is spent (not a failure)."""


def _billed(reader: CatalogReader) -> int | None:
    """The reader's billed metadata bytes so far, or None when it reports none."""
    value = getattr(reader, "bytes_billed", None)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _h(ref: ObjectRef) -> int:
    d = hashlib.blake2b(ref.canonical().encode(), digest_size=8).digest()
    return int.from_bytes(d, "big", signed=True)


def _cat(catalog: str | None) -> str:
    return _NO_CATALOG if catalog is None else catalog


def _scope_hash(cfg: CrawlConfig) -> str:
    text = repr((tuple(cfg.include), tuple(cfg.exclude)))
    return hashlib.blake2b(text.encode(), digest_size=8).hexdigest()


def _parse_ts(value: object) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else None


def _chunks(items: Sequence[_T], n: int) -> list[Sequence[_T]]:
    return [items[i : i + n] for i in range(0, len(items), max(1, n))]


class _Seen:
    """Names seen this run, on disk: memory does not grow with the number of tables."""

    def __init__(self, directory: Path) -> None:
        self._c = sqlite3.connect(str(directory / "seen.db"), isolation_level=None)
        self._c.execute("PRAGMA journal_mode=OFF")
        self._c.execute("PRAGMA synchronous=OFF")
        self._c.execute("CREATE TABLE t(h INTEGER PRIMARY KEY) WITHOUT ROWID")
        self._c.execute(
            "CREATE TABLE s(cat TEXT, name TEXT, complete INTEGER, PRIMARY KEY(cat, name)) "
            "WITHOUT ROWID"
        )
        # the schemas the store held when a resumed run started (filled only on resume)
        self._c.execute(
            "CREATE TABLE stored(cat TEXT, name TEXT, PRIMARY KEY(cat, name)) WITHOUT ROWID"
        )
        # the views whose definitions the lineage phase reads (canonical refs)
        self._c.execute("CREATE TABLE v(ref TEXT PRIMARY KEY)")

    def add_tables(self, hashes: Iterable[int]) -> None:
        self._c.execute("BEGIN")
        self._c.executemany("INSERT OR IGNORE INTO t VALUES (?)", ((h,) for h in hashes))
        self._c.execute("COMMIT")

    def has_table(self, h: int) -> bool:
        return self._c.execute("SELECT 1 FROM t WHERE h = ?", (h,)).fetchone() is not None

    def add_schema(self, catalog: str | None, name: str) -> None:
        self._c.execute("INSERT OR IGNORE INTO s VALUES (?, ?, 0)", (_cat(catalog), name))

    def complete_schema(self, catalog: str | None, name: str) -> None:
        self._c.execute(
            "UPDATE s SET complete = 1 WHERE cat = ? AND name = ?", (_cat(catalog), name)
        )

    def schema_state(self, catalog: str | None, name: str) -> int | None:
        """``None`` unseen, ``0`` seen but not completely listed this run, ``1`` complete."""
        row = self._c.execute(
            "SELECT complete FROM s WHERE cat = ? AND name = ?", (_cat(catalog), name)
        ).fetchone()
        return None if row is None else int(row[0])

    def add_stored(self, schemas: Iterable[Schema]) -> None:
        self._c.execute("BEGIN")
        self._c.executemany(
            "INSERT OR IGNORE INTO stored VALUES (?, ?)",
            ((_cat(s.catalog), s.name) for s in schemas),
        )
        self._c.execute("COMMIT")

    def is_stored(self, catalog: str | None, name: str) -> bool:
        row = self._c.execute(
            "SELECT 1 FROM stored WHERE cat = ? AND name = ?", (_cat(catalog), name)
        ).fetchone()
        return row is not None

    def add_views(self, refs: Iterable[str]) -> None:
        self._c.execute("BEGIN")
        self._c.executemany("INSERT OR IGNORE INTO v VALUES (?)", ((r,) for r in refs))
        self._c.execute("COMMIT")

    def views_after(self, last: str, n: int) -> list[str]:
        rows = self._c.execute(
            "SELECT ref FROM v WHERE ref > ? ORDER BY ref LIMIT ?", (last, max(1, n))
        ).fetchall()
        return [str(r[0]) for r in rows]

    def close(self) -> None:
        self._c.close()


def _view_lineage_facts(store: Store, views: Sequence[ObjectRef]) -> list[Fact]:
    """The ``view_definition`` lineage facts of ``views`` and of every column under them,
    including columns the view no longer has. A store without a lineage index is asked for the
    stored columns only."""
    index = lineage_index(store)
    if index is not None:
        facts = index.lineage_facts_under(views)
    else:
        subjects: list[ObjectRef] = []
        for v in views:
            subjects.append(v)
            subjects.extend(c.ref for c in store.columns(v))
        facts = store.facts_for(subjects, LINEAGE_KINDS)
    return [f for f in facts if f.source == VIEW_SOURCE]


def _unavailable_diagnostic(target: ObjectRef) -> Diagnostic:
    """SG211 for a view whose definition the reader did not return (a fixed category)."""
    return Diagnostic(
        "SG211",
        Severity.WARNING,
        f"lineage could not be parsed for {target.canonical()}: definition_unavailable",
        location=target.canonical(),
    )


def _resolution_fingerprint(store: Store, names: Sequence[RawName], target: ObjectRef) -> str:
    """How ``names`` (a view's relation names, as written) resolve in ``store`` now: per name,
    the stored relation it reaches and that relation's columns, or nothing (unresolved). A
    name entering scope, leaving it, or an upstream gaining/losing a column changes it."""
    resolved = resolve_relation_names(store, names, target)
    rows = sorted(
        json.dumps(
            [list(n), *((r.ref, list(r.columns)) if (r := resolved.get(n)) else (None, None))]
        )
        for n in set(names)
    )
    return hashlib.sha256("\n".join(rows).encode("utf-8", "surrogatepass")).hexdigest()


def _encode_state(sha: str, names: Sequence[RawName], fingerprint: str) -> str:
    """The ``lineage_state`` value: the definition's sha256, the relation names it reads and
    the fingerprint of how they resolved when it was parsed."""
    return json.dumps(
        {"sha256": sha, "names": sorted(list(n) for n in set(names)), "resolution": fingerprint},
        sort_keys=True,
        separators=(",", ":"),
    )


def _state_current(store: Store, ref: ObjectRef, stored: str | None, sha: str | None) -> bool:
    """Whether a view's stored lineage is still current: its definition is unchanged (``sha``
    None: not known yet, so not judged) AND its relation names still resolve as they did. A
    state from before 0.2.0's resolution fingerprint (a bare sha256) is never current: its
    names are unknown, so the view is parsed once more and the new state written."""
    if stored is None or not stored.startswith("{"):
        return False
    try:
        state = json.loads(stored)
        names: list[RawName] = [(n[0], n[1], n[2]) for n in state["names"]]
        old_sha, old_fp = str(state["sha256"]), str(state["resolution"])
    except (ValueError, KeyError, TypeError, IndexError):
        return False
    if sha is not None and old_sha != sha:
        return False
    return _resolution_fingerprint(store, names, ref) == old_fp


def _retract_view_lineage(store: Store, views: Sequence[ObjectRef]) -> None:
    """Withdrawn views lose their parsed lineage and their stored definition hash."""
    index = lineage_index(store)
    for chunk in _chunks(views, 500):
        with store.transaction():
            old = _view_lineage_facts(store, chunk)
            if old:
                store.delete_facts(sorted({f.id for f in old}))
            if index is not None:
                index.lineage_state_set(dict.fromkeys(chunk))


class _Crawl:
    def __init__(
        self,
        reader: CatalogReader,
        store: Store,
        config: CrawlConfig,
        resume: bool,
        incremental: bool,
        now: Callable[[], datetime],
        seen: _Seen,
        crawl_bytes: int | None = None,
    ) -> None:
        self.reader, self.store, self.cfg, self.now, self.seen = reader, store, config, now, seen
        # Budget.crawl_bytes: admitted against what the reader reports billed during THIS crawl
        self.crawl_bytes = crawl_bytes
        self.bytes0 = _billed(reader)
        self.filter = ScopeFilter(config.include, config.exclude)
        self.caps = reader.capabilities()
        self.errors: list[Diagnostic] = []
        self.notes: list[Diagnostic] = []  # reader notes: reported, but not a failed crawl
        self.lineage_diagnostics: list[Diagnostic] = []  # never a failed crawl
        self.views_parsed = 0
        self._qlock = threading.Lock()
        self.queries = 0
        self.tables_seen = 0
        self.tables_written = 0
        self.complete_catalogs: set[str | None] = set()
        self.listed_catalogs: list[str | None] = []
        self.catalogs_ok = True
        self.started = now()
        self.changed_since: datetime | None = None
        self.scope = _scope_hash(config)
        # resume cursor: the schema that was in progress (or last completed) when interrupted
        self.cursor: dict[str, Any] | None = None
        self._skipping = False

        state = store.crawl_state_get(STATE_KEY) if resume else None
        if (
            state is not None
            and state.get("status") == "running"
            and state.get("scope") == self.scope  # a cursor from another scope is discarded
        ):
            cur = state.get("cursor")
            self.cursor = dict(cur) if isinstance(cur, dict) else None
            self.changed_since = _parse_ts(state.get("changed_since"))
            self.started = _parse_ts(state.get("started_at")) or self.started
        else:
            last = store.crawl_state_get(LAST_KEY)
            if (
                incremental
                and self.caps.incremental
                and last is not None
                and last.get("scope") == self.scope  # a changed scope invalidates the baseline
            ):
                self.changed_since = _parse_ts(last.get("started_at"))
        # the cursor a resumed run skips to; ``self.cursor`` moves on as this run checkpoints
        self.resume_at = self.cursor
        # views written before the interruption are not re-listed: lineage offers every view
        self.resumed = self.cursor is not None
        self._skipping = self.cursor is not None
        if self._skipping:  # a schema listed before the cursor but never stored is crawled
            seen.add_stored(store.iter_schemas())
        self._checkpoint()

    # -- bookkeeping -------------------------------------------------------------------------

    def spent_bytes(self) -> int | None:
        now = _billed(self.reader)
        return None if now is None else now - (self.bytes0 or 0)

    def _admit(self) -> None:
        """Before each listing query: stop once ``crawl_bytes`` is spent. A reader that reports
        no bytes is never counted (its crawl is not capped)."""
        spent = self.spent_bytes()
        if self.crawl_bytes is not None and spent is not None and spent >= self.crawl_bytes:
            raise _OverCrawlBudget(spent)

    def _count(self, n: int = 1) -> None:
        with self._qlock:
            self.queries += n

    def drain_reader_diagnostics(self) -> None:
        """Move what the reader noted (``reader.diagnostics``, duck-typed) into the report, once."""
        notes = getattr(self.reader, "diagnostics", None)
        if not isinstance(notes, list):
            return
        with self._qlock:
            taken, notes[:] = list(notes), []
        self.notes.extend(d for d in taken if isinstance(d, Diagnostic))

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
                "scope": self.scope,
                "cursor": self.cursor,
            },
        )

    # -- listing -----------------------------------------------------------------------------

    def run(self, pool: ThreadPoolExecutor) -> int:
        catalogs: list[str | None] = []
        self._admit()
        try:
            self._count()
            catalogs = list(self.reader.list_catalogs())
        except Exception as e:
            self._error("list_catalogs", e)
            self.catalogs_ok = False
        self.listed_catalogs = list(catalogs) if catalogs else [None]
        cursor_catalog = self.resume_at.get("catalog") if self.resume_at else None
        for catalog in self.listed_catalogs:
            if self._skipping and catalog != cursor_catalog:
                self._crawl_catalog(catalog, pool)  # before the cursor: skipped schema by schema
                continue
            self._crawl_catalog(catalog, pool)
            if self._skipping:  # the cursor's schema is gone: redo this catalog from the top
                self._skipping = False
                self._crawl_catalog(catalog, pool)
        if self._skipping:  # the cursor's catalog is gone: nothing was crawled, so crawl it all
            self._skipping = False
            for catalog in self.listed_catalogs:
                self._crawl_catalog(catalog, pool)
        return self._withdraw() if self.catalogs_ok else 0

    def _crawl_catalog(self, catalog: str | None, pool: ThreadPoolExecutor) -> None:
        token: str | None = None
        while True:
            self._admit()
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
        if self.filter.excludes_schema(schema.catalog, schema.name):
            return
        self.seen.add_schema(schema.catalog, schema.name)
        resume_token: str | None = None
        if self._skipping:
            cur = self.resume_at or {}
            if (cur.get("catalog"), cur.get("schema")) != (schema.catalog, schema.name):
                if self.seen.is_stored(schema.catalog, schema.name):
                    return  # listed before the cursor: done in the interrupted run
                # listed before the cursor yet never stored: created since, crawl it whole
            else:
                self._skipping = False
                if cur.get("done"):
                    return
                resume_token = cur.get("page_token")
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
        self.cursor = {
            "catalog": schema.catalog,
            "schema": schema.name,
            "page_token": None,
            "done": True,
        }
        self._checkpoint()
        if names_ok and (incremental or resume_token is None):
            self.seen.complete_schema(schema.catalog, schema.name)

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
            self._admit()
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
                self.seen.add_tables(_h(t.ref) for t in tables)
            if process:
                with self.store.transaction():
                    self._write_page(tables, loc, pool)
                    if page.next_token is not None:
                        self.cursor = {
                            "catalog": schema.catalog,
                            "schema": schema.name,
                            "page_token": page.next_token,
                            "done": False,
                        }
                        self._checkpoint()
            else:
                # name-only walk of an incremental run: an unchanged table the store lacks or
                # holds withdrawn (schema reappeared, scope widened, table restored) is re-read
                missing = [t for t in tables if self._needs_restore(t.ref)]
                if missing:
                    with self.store.transaction():
                        self._write_page(missing, loc, pool)
            token = page.next_token
            if token is None:
                return True

    def _needs_restore(self, ref: ObjectRef) -> bool:
        stored = self.store.table(ref)
        return stored is None or stored.withdrawn_at is not None

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
            self.drain_reader_diagnostics()
        return cols, cons

    def _write_page(self, tables: list[Table], loc: str, pool: ThreadPoolExecutor) -> None:
        if not tables:
            return
        self.store.upsert_tables(tables)
        self.tables_written += len(tables)
        self.seen.add_views(t.ref.canonical() for t in tables if t.kind in _VIEW_KINDS)
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
        """One streaming pass over the store: only schemas listed completely this run are judged
        by name; a schema/catalog that vanished (or fell out of scope) is withdrawn whole; a
        schema whose listing failed or was resumed mid-way is left alone."""
        gone: list[ObjectRef] = []
        gone_views: list[ObjectRef] = []
        for t in self.store.iter_tables():
            ref = t.ref
            state = self.seen.schema_state(ref.catalog, ref.schema or "")
            if state == 1:
                if self.seen.has_table(_h(ref)):
                    continue
            elif state is not None or (
                ref.catalog not in self.complete_catalogs and ref.catalog in self.listed_catalogs
            ):
                continue
            gone.append(ref)
            if t.kind in _VIEW_KINDS:
                gone_views.append(ref)
        if gone:
            self.store.mark_withdrawn(gone, self.now())
        if gone_views:
            _retract_view_lineage(self.store, gone_views)
        return len(gone)

    # -- view lineage (0.2.0 spec 4.2-4.4) ---------------------------------------------------

    def lineage(self, parser_pool: ParserPool | None) -> None:
        """Parse the definitions of the views written this run and of the live views the store
        holds without a definition hash (a resumed crawl's earlier pages)."""
        reader = self.reader
        if not (reads_view_definitions(reader) and isinstance(reader, ViewDefinitionReader)):
            return
        if not lineage_available():  # no raw SQL is read for nothing
            self.lineage_diagnostics.append(extra_missing_diagnostic())
            return
        index = lineage_index(self.store)
        if index is not None or self.resumed:
            self._add_stored_views(index, every=self.resumed)
        pool = parser_pool if parser_pool is not None else ParserPool()
        try:
            last = ""
            while True:
                keys = self.seen.views_after(last, self.cfg.column_batch)
                if not keys:
                    return
                last = keys[-1]
                self._lineage_batch(reader, [ObjectRef.parse(k) for k in keys], index, pool)
        finally:
            if parser_pool is None:
                pool.close()

    def _add_stored_views(self, index: LineageIndex | None, *, every: bool) -> None:
        """One streaming pass over the store's live views in scope: all of them after a resume
        (an earlier run may have written a changed view whose old hash is still stored; the hash
        check skips the unchanged ones), otherwise those without a current lineage state (no
        definition hash, or relation names that no longer resolve as they did when parsed)."""
        chunk: list[ObjectRef] = []

        def flush() -> None:
            have = {} if every or index is None else index.lineage_state_get(chunk)
            self.seen.add_views(
                r.canonical() for r in chunk if not _state_current(self.store, r, have.get(r), None)
            )
            chunk.clear()

        for t in self.store.iter_tables():
            if t.kind in _VIEW_KINDS and self.filter.allows(t.ref):
                chunk.append(t.ref)
                if len(chunk) >= max(1, self.cfg.column_batch):
                    flush()
        if chunk:
            flush()

    def _lineage_batch(
        self,
        reader: ViewDefinitionReader,
        refs: list[ObjectRef],
        index: LineageIndex | None,
        pool: ParserPool,
    ) -> None:
        from schemaguru.pii.mask import definition_template  # pii is not below catalog

        self._admit()
        try:
            self._count()
            texts = dict(reader.view_definitions(refs))
        except Exception as e:
            # a driver's message can quote SQL or bind values: only the exception type is kept
            self.errors.append(
                Diagnostic(
                    "SG102",
                    Severity.ERROR,
                    f"crawl skipped the view definitions of {len(refs)} view(s): "
                    f"{type(e).__name__} (its message is not shown)",
                    location="view_definitions",
                )
            )
            return
        stored = index.lineage_state_get(refs) if index is not None else {}
        # skipped only when the definition is unchanged AND its names resolve as they did
        items = [
            DefinitionItem(r, texts[r], self.reader.dialect)
            for r in refs
            if r in texts
            and not _state_current(self.store, r, stored.get(r), definition_sha256(texts[r]))
        ]
        # a view the reader returned no definition for: its old lineage is no longer known
        missing = [r for r in refs if r not in texts]
        texts.clear()
        if not items and not missing:
            return
        try:
            results = (
                []
                if not items
                else analyse_definitions(
                    items,
                    resolve=partial(resolve_relation_names, self.store),
                    pool=pool,
                    template=definition_template,
                )
            )
        except Exception:  # the pool broke its contract; its message is not kept either
            results = [
                DefinitionResult(i.target, i.dialect, definition_sha256(i.sql), None,
                                 "worker_died", None)
                for i in items
            ]  # fmt: skip
        items.clear()  # the raw texts end here
        observed = self.now()
        facts: list[Fact] = []
        state: dict[ObjectRef, str | None] = {}
        for r in missing:
            self.lineage_diagnostics.append(_unavailable_diagnostic(r))
            state[r] = None
        for res in results:
            if res.parsed is None:  # retried next crawl; stale facts are retracted below
                self.lineage_diagnostics.append(
                    failure_diagnostic(res.target, res.failure or "parse_error")
                )
                state[res.target] = None
                continue
            cols = sorted(self.store.columns(res.target), key=lambda c: c.ordinal)
            facts.extend(
                lineage_facts(
                    res,
                    target_columns=[c.ref.column for c in cols if c.ref.column is not None],
                    provenance=Provenance.CATALOG,
                    source=VIEW_SOURCE,
                    observed_at=observed,
                    evidence=True,
                )
            )
            state[res.target] = _encode_state(
                res.sha256, res.names, _resolution_fingerprint(self.store, res.names, res.target)
            )
            self.views_parsed += 1
        with self.store.transaction():  # replace: old view facts out, new in, hash recorded
            old = _view_lineage_facts(self.store, [*missing, *(r.target for r in results)])
            if old:
                self.store.delete_facts(sorted({f.id for f in old}))
            if facts:
                self.store.put_facts(facts)
            if index is not None:
                index.lineage_state_set(state)


def crawl(
    reader: CatalogReader,
    store: Store,
    *,
    config: CrawlConfig | None = None,
    resume: bool = True,
    incremental: bool = True,
    now: Callable[[], datetime] = utcnow,
    crawl_bytes: int | None = None,
    lineage: bool = True,
    parser_pool: ParserPool | None = None,
) -> CrawlReport:
    """Crawl ``reader`` into ``store``.

    ``resume`` continues an interrupted crawl from its checkpoint: schemas listed before the
    saved cursor are skipped (readers must list schemas in a stable order; the shipped readers
    sort by name) unless the store holds no row for them (created since the interruption: crawled
    whole), the in-progress one restarts at its saved page. A checkpoint saved under a different
    include/exclude scope is discarded and the crawl starts from the top. ``incremental``
    re-reads only tables the reader reports as changed since the last crawl that finished without
    errors, plus a name-only listing per schema that detects drops and restores tables the
    store lacks or holds withdrawn; it is ignored when the include/exclude scope differs from
    that crawl's. A table is withdrawn only when a complete listing of its schema (from the first
    page, this run) omits it, or its schema/catalog is gone or now out of scope. If the reader
    has a ``page_size`` / ``timeout_s`` attribute it is set from ``config``.

    ``crawl_bytes`` (``Budget.crawl_bytes``) caps the metadata bytes the warehouse bills during
    this crawl, as the reader reports them (``reader.bytes_billed``; BigQuery). Before each
    listing query the bytes billed so far are compared with the cap; once it is reached the
    crawl stops between pages with a ``SG110`` warning and keeps its checkpoint, so the next
    ``crawl()`` resumes where it stopped. A reader that reports no bytes is not counted.

    ``lineage`` parses view definitions into ``LINEAGE`` / ``COLUMN_LINEAGE`` facts (source
    ``view_definition``) when the reader reads them (``capabilities().view_definitions``); a
    view's facts are replaced in one transaction when its definition hash changed and left as
    they are otherwise; a withdrawn view's are retracted. Parse failures (``SG211``) and a
    missing ``lineage`` extra (``SG212``, once) go to ``lineage_diagnostics``, which never
    blocks the incremental baseline. ``parser_pool`` is used and left open; without one the
    crawl starts its own and closes it before returning.
    """
    cfg = config or CrawlConfig()
    t0 = time.monotonic()
    if isinstance(getattr(reader, "page_size", None), int):
        reader.page_size = cfg.page_size  # type: ignore[attr-defined]
    if hasattr(reader, "timeout_s"):
        reader.timeout_s = cfg.timeout_s
    with tempfile.TemporaryDirectory(prefix="schemaguru-crawl-") as tmp:
        seen = _Seen(Path(tmp))
        stopped = False
        try:
            run = _Crawl(reader, store, cfg, resume, incremental, now, seen, crawl_bytes)
            run.drain_reader_diagnostics()  # a note left by an earlier use is not this crawl's
            run.notes.clear()
            with ThreadPoolExecutor(max_workers=max(1, cfg.workers)) as pool:
                try:
                    withdrawn = run.run(pool)
                except _OverCrawlBudget:
                    stopped, withdrawn = True, 0  # an incomplete listing withdraws nothing
            lineage_t0 = time.monotonic()
            if lineage and not stopped:
                try:
                    run.lineage(parser_pool)
                except _OverCrawlBudget:
                    stopped = True
            lineage_s = time.monotonic() - lineage_t0
        finally:
            seen.close()
    run.drain_reader_diagnostics()
    bytes_billed = run.spent_bytes()
    if stopped:
        run.errors.append(
            Diagnostic(
                "SG110",
                Severity.WARNING,
                f"the crawl stopped at Budget.crawl_bytes ({crawl_bytes} bytes; {bytes_billed} "
                "billed); its checkpoint is kept, so the next crawl resumes where it stopped",
                hint="raise budget.crawl_bytes, or narrow crawl.include",
            )
        )
    if not run.errors:
        store.crawl_state_set(LAST_KEY, {"started_at": to_iso(run.started), "scope": run.scope})
    if not stopped:  # a stopped crawl stays "running": crawl(resume=True) continues it
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
    if bytes_billed is not None:
        store.ledger_add("crawl", bytes_billed, "bytes", "catalog crawl")
    return CrawlReport(
        tables_seen=run.tables_seen,
        tables_written=run.tables_written,
        withdrawn=withdrawn,
        errors=[*run.errors, *run.notes],
        queries=run.queries,
        bytes_billed=bytes_billed,
        bytes_billed_is_estimate=getattr(reader, "bytes_billed_is_lower_bound", False) is True,
        duration_s=time.monotonic() - t0,
        views_parsed=run.views_parsed,
        lineage_s=lineage_s,
        lineage_diagnostics=list(run.lineage_diagnostics),
    )
