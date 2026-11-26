"""``SQLiteStore``: the default single-file store (spec 7.2)."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import struct
import threading
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from schemaguru._util.hashing import canonical_json
from schemaguru._util.text import name_tokens
from schemaguru._util.timeutil import to_iso, utcnow
from schemaguru.diagnostics import CODES, Diagnostic, DiagnosticBag, Severity
from schemaguru.errors import SchemaGuruError, StoreVersionError
from schemaguru.model.facts import Fact
from schemaguru.model.joins import JoinRef, subject_key
from schemaguru.model.kinds import LINEAGE_KINDS
from schemaguru.model.lineage import Level, LineageEdge, edges_of, resolve_lineage
from schemaguru.model.refs import ObjectRef
from schemaguru.model.structure import Column, Constraint, Schema, Table
from schemaguru.store import schema_sql
from schemaguru.store.protocol import LedgerUnit, LineageDirection
from schemaguru.store.search_docs import (
    GLOSSARY,
    INDEXED_KINDS,
    MEANING_KINDS,
    doc_tables,
    glossary_tables,
    stored_doc_tables,
    strings,
)

log = logging.getLogger("schemaguru.store")

_PAGE = 1000
_CHUNK = 500
_FTS_WEIGHTS = "0.0, 10.0, 3.0, 1.0"  # ref, name_tokens, columns, text
# crawl_state key naming the search backend that last wrote the index ("fts5" or "like")
SEARCH_BACKEND_KEY = "_search_backend"
_LINEAGE_VERSION = 3  # the schema version that added lineage_edges
# the fact kind holding each lineage level, and back
_LINEAGE_KIND: dict[str, str] = {"table": "LINEAGE", "column": "COLUMN_LINEAGE"}
_LINEAGE_LEVEL: dict[str, str] = {v: k for k, v in _LINEAGE_KIND.items()}
_EMPTY_INDEX = "the read-only store's search index is empty; open it read-write once to rebuild it"


def _fts5_available(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("CREATE VIRTUAL TABLE temp._fts5_probe USING fts5(x)")
        conn.execute("DROP TABLE temp._fts5_probe")
    except sqlite3.Error:
        return False
    return True


_dumps = canonical_json
_subject_key = subject_key


def _table_from_key(key: str) -> ObjectRef:
    return ObjectRef.parse(key, has_column=False)


def _chunks(items: Sequence[str]) -> Iterator[Sequence[str]]:
    for i in range(0, len(items), _CHUNK):
        yield items[i : i + _CHUNK]


def _marks(n: int) -> str:
    return ",".join("?" * n)


def _like(token: str) -> str:
    return "%" + token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


class SQLiteStore:
    """One SQLite file, WAL mode, one connection guarded by a re-entrant lock.

    Every public write runs in a transaction; nest freely inside :meth:`transaction`.
    Search uses FTS5 when the local SQLite has it, otherwise a LIKE scan over ``docs``
    (diagnostic ``SG701`` in :attr:`diagnostics`).
    """

    def __init__(self, path: str | os.PathLike[str], *, readonly: bool = False) -> None:
        self.path = os.fspath(path)
        self.readonly = readonly
        self.diagnostics = DiagnosticBag()
        self._lock = threading.RLock()
        self._depth = 0
        if readonly:
            uri = Path(self.path).resolve().as_uri() + "?mode=ro"
            self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        else:
            if self.path != ":memory:":
                Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.isolation_level = None  # explicit BEGIN/COMMIT only
        try:
            self._conn.execute("PRAGMA busy_timeout=30000")
            if not readonly:
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=NORMAL")
            self._migrate()
            self._fts = self._setup_search()
        except BaseException:
            self._conn.close()
            raise

    # ----- lifecycle ---------------------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> SQLiteStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _migrate(self) -> None:
        conn = self._conn
        have = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
        ).fetchone()
        current = 0
        if have:
            current = conn.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_version"
            ).fetchone()[0]
        self._check_version(current)
        if current == schema_sql.LATEST_VERSION:
            return
        if self.readonly:
            raise StoreVersionError(
                f"store at {self.path!r} is at schema version {current}, library needs "
                f"{schema_sql.LATEST_VERSION}; open it read-write once to migrate it"
            )
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(schema_sql.VERSION_DDL)
            current = conn.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_version"
            ).fetchone()[0]
            self._check_version(current)  # another process may have migrated meanwhile
            for version, sql in schema_sql.migrations():
                if version <= current:
                    continue
                for stmt in schema_sql.statements(sql):
                    conn.execute(stmt)
                conn.execute(
                    "INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
                    (version, to_iso(utcnow())),
                )
            if current < _LINEAGE_VERSION:  # lineage facts written before the index existed
                self._relineage(
                    {
                        (key, _LINEAGE_LEVEL[kind])
                        for kind, key in conn.execute(
                            f"SELECT kind, subject_key FROM facts WHERE kind IN ({_marks(2)})",
                            sorted(LINEAGE_KINDS),
                        )
                    }
                )
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    def _check_version(self, current: int) -> None:
        if current > schema_sql.LATEST_VERSION:
            raise StoreVersionError(
                f"SG702: store at {self.path!r} has schema version {current}, but this "
                f"SchemaGuru supports up to {schema_sql.LATEST_VERSION}; upgrade SchemaGuru "
                "or use a different store file"
            )

    def _setup_search(self) -> bool:
        """Pick the search backend and make sure its index covers the stored tables.

        The backend that last wrote the index is recorded (``SEARCH_BACKEND_KEY``): writes made
        under the other backend never reached this one's index, so a switch rebuilds it. A
        read-only store whose index is empty while it holds tables warns ``SG706``.
        """
        if not _fts5_available(self._conn):
            self._warn_fallback()
            fts = False
        else:
            exists = self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='table_docs'"
            ).fetchone()
            fts = bool(exists)
            if not exists:
                if self.readonly:
                    self._warn_fallback("the store has no FTS5 index and is read-only")
                else:
                    self._conn.execute(schema_sql.FTS_DDL)
                    fts = True
        self._fts = fts
        backend = "fts5" if fts else "like"
        if self.readonly:
            if self._index_empty():
                self.diagnostics.add(
                    Diagnostic("SG706", Severity.WARNING, _EMPTY_INDEX, location=self.path)
                )
                log.warning("SG706 %s", _EMPTY_INDEX)
            return fts
        recorded = (self.crawl_state_get(SEARCH_BACKEND_KEY) or {}).get("backend")
        if recorded is not None and recorded != backend:
            self._rebuild_index()  # written under the other backend since: stale
        elif self._index_empty():
            self._rebuild_index()  # e.g. a store built under the other backend before 0.2.0
        if recorded != backend:
            self.crawl_state_set(SEARCH_BACKEND_KEY, {"backend": backend})
        return fts

    def _index_empty(self) -> bool:
        """The store holds tables but the current backend's index holds no document."""
        index = "table_docs" if self._fts else "docs"
        with self._lock:
            if self._conn.execute(f"SELECT 1 FROM {index} LIMIT 1").fetchone():
                return False
            return bool(self._conn.execute("SELECT 1 FROM tables LIMIT 1").fetchone())

    def _rebuild_index(self) -> None:
        """Clear the current backend's index and reindex every table."""
        index = "table_docs" if self._fts else "docs"
        with self.transaction():
            self._conn.execute(f"DELETE FROM {index}")
            for (key,) in self._conn.execute("SELECT ref FROM tables ORDER BY ref").fetchall():
                self._reindex(key)

    def _warn_fallback(self, why: str = "") -> None:
        msg = f"{CODES['SG701']}" + (f" ({why})" if why else "")
        msg += "; install a SQLite build with FTS5 for ranked search"
        self.diagnostics.add(Diagnostic("SG701", Severity.WARNING, msg, location=self.path))
        log.warning("SG701 %s", msg)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """One write transaction. A nested call is a ``SAVEPOINT``: an exception that leaves the
        nested block rolls back only that block's writes, and an outer block that catches it
        can still commit its own. A failing ``COMMIT`` rolls the transaction back and re-raises,
        so the next transaction starts clean."""
        if self.readonly:
            raise SchemaGuruError("the store was opened read-only")
        with self._lock:
            if self._depth:
                name = f"sg_tx_{self._depth}"
                self._conn.execute(f"SAVEPOINT {name}")
                self._depth += 1
                try:
                    yield
                except BaseException:
                    if self._conn.in_transaction:
                        self._conn.execute(f"ROLLBACK TO {name}")
                        self._conn.execute(f"RELEASE {name}")
                    raise
                else:
                    self._conn.execute(f"RELEASE {name}")
                finally:
                    self._depth -= 1
                return
            self._conn.execute("BEGIN IMMEDIATE")
            self._depth = 1
            try:
                yield
                self._conn.execute("COMMIT")
            except BaseException:
                self._rollback()
                raise
            finally:
                self._depth = 0

    def _rollback(self) -> None:
        """Roll back whatever is open; a failing ROLLBACK never hides the original error."""
        if not self._conn.in_transaction:
            return
        try:
            self._conn.execute("ROLLBACK")
        except sqlite3.Error:
            log.exception("ROLLBACK failed")

    def _q(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # ----- structure ---------------------------------------------------------------------

    def upsert_schemas(self, items: Iterable[Schema]) -> None:
        with self.transaction():
            self._conn.executemany(
                "INSERT INTO schemas(key, data) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET data=excluded.data",
                [(s.key(), _dumps(s.to_dict())) for s in items],
            )

    def iter_schemas(self) -> Iterator[Schema]:
        """Every schema in key order, read in pages of ``_PAGE`` (memory bounded by a page)."""
        last = ""
        while True:
            rows = self._q(
                "SELECT key, data FROM schemas WHERE key > ? ORDER BY key LIMIT ?", (last, _PAGE)
            )
            if not rows:
                return
            for _key, data in rows:
                yield Schema.from_dict(json.loads(data))
            last = rows[-1][0]

    def upsert_tables(self, items: Iterable[Table]) -> None:
        with self.transaction():
            for t in items:
                d = t.to_dict()
                del d["withdrawn_at"]
                key = t.ref.canonical()
                self._conn.execute(
                    "INSERT INTO tables(ref, ref_lower, name_lower, data, withdrawn_at) "
                    "VALUES (?, ?, lower(?), ?, ?) "
                    "ON CONFLICT(ref) DO UPDATE SET data=excluded.data, "
                    "withdrawn_at=excluded.withdrawn_at",
                    (
                        key,
                        key.lower(),
                        t.ref.table,
                        _dumps(d),
                        None if t.withdrawn_at is None else to_iso(t.withdrawn_at),
                    ),
                )
                self._reindex(key)

    def _row_to_table(self, data: str, withdrawn_at: str | None) -> Table:
        d = json.loads(data)
        d["withdrawn_at"] = withdrawn_at
        if withdrawn_at is not None:
            d["withdrawn_at"] = (
                datetime.strptime(withdrawn_at, "%Y-%m-%dT%H:%M:%S.%fZ")
                .replace(tzinfo=timezone.utc)
                .isoformat()
            )
        return Table.from_dict(d)

    def table(self, ref: ObjectRef) -> Table | None:
        rows = self._q(
            "SELECT data, withdrawn_at FROM tables WHERE ref=?", (ref.table_ref().canonical(),)
        )
        return self._row_to_table(*rows[0]) if rows else None

    def iter_tables(self, *, include_withdrawn: bool = False) -> Iterator[Table]:
        last = ""
        where = "" if include_withdrawn else "AND withdrawn_at IS NULL"
        while True:
            rows = self._q(
                f"SELECT ref, data, withdrawn_at FROM tables WHERE ref > ? {where} "
                "ORDER BY ref LIMIT ?",
                (last, _PAGE),
            )
            if not rows:
                return
            for _ref, data, wat in rows:
                yield self._row_to_table(data, wat)
            last = rows[-1][0]

    def upsert_columns(self, table: ObjectRef, cols: Sequence[Column]) -> None:
        key = table.table_ref().canonical()
        with self.transaction():
            self._conn.execute("DELETE FROM columns WHERE table_ref=?", (key,))
            self._conn.executemany(
                "INSERT OR REPLACE INTO columns(table_ref, name, ordinal, data) VALUES (?,?,?,?)",
                [(key, c.ref.column, c.ordinal, _dumps(c.to_dict())) for c in cols],
            )
            self._reindex(key)

    def columns(self, ref: ObjectRef) -> list[Column]:
        rows = self._q(
            "SELECT data FROM columns WHERE table_ref=? ORDER BY ordinal, name",
            (ref.table_ref().canonical(),),
        )
        return [Column.from_dict(json.loads(r[0])) for r in rows]

    def upsert_constraints(self, table: ObjectRef, cons: Sequence[Constraint]) -> None:
        key = table.table_ref().canonical()
        with self.transaction():
            self._conn.execute("DELETE FROM constraints WHERE table_ref=?", (key,))
            self._conn.executemany(
                "INSERT INTO constraints(table_ref, seq, data) VALUES (?,?,?)",
                [(key, i, _dumps(c.to_dict())) for i, c in enumerate(cons)],
            )

    def constraints(self, ref: ObjectRef) -> list[Constraint]:
        rows = self._q(
            "SELECT data FROM constraints WHERE table_ref=? ORDER BY seq",
            (ref.table_ref().canonical(),),
        )
        return [Constraint.from_dict(json.loads(r[0])) for r in rows]

    def mark_withdrawn(self, refs: Iterable[ObjectRef], at: datetime) -> None:
        stamp = to_iso(at)  # rejects naive datetimes
        with self.transaction():
            self._conn.executemany(
                "UPDATE tables SET withdrawn_at=COALESCE(withdrawn_at, ?) WHERE ref=?",
                [(stamp, r.table_ref().canonical()) for r in refs],
            )

    # ----- resolve -----------------------------------------------------------------------

    def resolve(self, text: str) -> tuple[ObjectRef | None, list[ObjectRef]]:
        try:
            ref = ObjectRef.parse(text)
        except ValueError:
            return None, []
        if ref.column is None:
            found, cands = self._resolve_table(ref)
            if found is not None or cands:
                return found, cands
            try:
                with_col = ObjectRef.parse(text, has_column=True)
            except ValueError:
                return None, []
        else:
            with_col = ref
        assert with_col.column is not None
        found, cands = self._resolve_table(with_col.table_ref())
        if found is None:
            return None, cands  # ambiguous or unknown table; do not guess a column
        return self._resolve_column(found, with_col.column)

    def _resolve_table(self, ref: ObjectRef) -> tuple[ObjectRef | None, list[ObjectRef]]:
        key = ref.canonical()
        if self._q("SELECT 1 FROM tables WHERE ref=?", (key,)):
            return ref, []
        rows = self._q("SELECT ref FROM tables WHERE ref_lower=? ORDER BY ref", (key.lower(),))
        refs = [_table_from_key(r[0]) for r in rows]
        if len(refs) == 1:
            return refs[0], []
        return None, refs

    def _resolve_column(
        self, table: ObjectRef, column: str
    ) -> tuple[ObjectRef | None, list[ObjectRef]]:
        names = [
            r[0]
            for r in self._q(
                "SELECT name FROM columns WHERE table_ref=? ORDER BY name", (table.canonical(),)
            )
        ]

        def with_col(n: str) -> ObjectRef:
            return ObjectRef(table.catalog, table.schema, table.table, n)

        if column in names:
            return with_col(column), []
        ci = [n for n in names if n.lower() == column.lower()]
        if len(ci) == 1:
            return with_col(ci[0]), []
        return None, [with_col(n) for n in ci]

    # ----- facts -------------------------------------------------------------------------

    def put_facts(self, facts: Iterable[Fact]) -> None:
        with self.transaction():
            touched: set[str] = set()
            rows = []
            indexed: list[str] = []
            lineage: set[tuple[str, str]] = set()
            lineage_ids: list[str] = []
            for f in facts:
                key = _subject_key(f.subject)
                rows.append((f.id, f.kind.name, key, _dumps(f.to_dict())))
                if f.kind.name in INDEXED_KINDS:
                    touched |= doc_tables(f.kind.name, f.subject, f.value)
                    indexed.append(f.id)
                if f.kind.name in LINEAGE_KINDS:
                    lineage.add((key, _LINEAGE_LEVEL[f.kind.name]))
                    lineage_ids.append(f.id)
            lineage |= self._stored_lineage_subjects(lineage_ids)  # a replaced row's own subject
            # a fact replaced under the same id may have fed other tables' docs (a subject that
            # moved, a term that no longer maps there): those docs are rebuilt too
            for chunk in _chunks(sorted(set(indexed))):
                for (data,) in self._conn.execute(
                    f"SELECT data FROM facts WHERE id IN ({_marks(len(chunk))})", list(chunk)
                ):
                    touched |= stored_doc_tables(data)
            self._conn.executemany(
                "INSERT INTO facts(id, kind, subject_key, data) VALUES (?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET kind=excluded.kind, "
                "subject_key=excluded.subject_key, data=excluded.data",
                rows,
            )
            for key in sorted(touched):
                self._reindex(key)
            self._relineage(lineage)

    def delete_facts(self, ids: Iterable[str]) -> int:
        wanted = sorted(set(ids))
        if not wanted:
            return 0
        removed = 0
        with self.transaction():
            touched: set[str] = set()
            lineage: set[tuple[str, str]] = set()
            for chunk in _chunks(wanted):
                marks = _marks(len(chunk))
                for kind, key, data in self._q(
                    f"SELECT kind, subject_key, data FROM facts WHERE id IN ({marks})", chunk
                ):
                    removed += 1
                    touched |= stored_doc_tables(data)
                    if kind in _LINEAGE_LEVEL:
                        lineage.add((key, _LINEAGE_LEVEL[kind]))
                self._conn.execute(f"DELETE FROM facts WHERE id IN ({marks})", list(chunk))
            for key in sorted(touched):
                self._reindex(key)
            self._relineage(lineage)
        return removed

    # ----- lineage index -----------------------------------------------------------------

    def _stored_lineage_subjects(self, ids: Sequence[str]) -> set[tuple[str, str]]:
        """``(subject_key, level)`` of the stored lineage rows among ``ids``."""
        out: set[tuple[str, str]] = set()
        for chunk in _chunks(sorted(set(ids))):
            for kind, key in self._conn.execute(
                f"SELECT kind, subject_key FROM facts WHERE id IN ({_marks(len(chunk))})", chunk
            ):
                if kind in _LINEAGE_LEVEL:
                    out.add((key, _LINEAGE_LEVEL[kind]))
        return out

    def _relineage(self, subjects: set[tuple[str, str]]) -> None:
        """Recompute the effective edges of each ``(subject_key, level)`` from every lineage fact
        of it, as stored now. Runs inside the caller's transaction."""
        if not subjects:
            return
        conn = self._conn
        conn.executemany(
            "DELETE FROM lineage_edges WHERE warehouse='' AND downstream_key=? AND level=?",
            sorted(subjects),
        )
        facts: list[Fact] = []
        for level, kind in sorted(_LINEAGE_KIND.items()):
            keys = sorted(k for k, lv in subjects if lv == level)
            for chunk in _chunks(keys):
                for (data,) in conn.execute(
                    f"SELECT data FROM facts WHERE subject_key IN ({_marks(len(chunk))}) "
                    "AND kind=?",
                    [*chunk, kind],
                ):
                    facts.append(Fact.from_dict(json.loads(data)))
        conn.executemany(
            "INSERT INTO lineage_edges(upstream_key, downstream_key, level, fact_id) "
            "VALUES (?,?,?,?)",
            [
                (e.upstream_key, e.downstream_key, e.level, e.fact_id)
                for e in edges_of(resolve_lineage(facts))
            ],
        )

    def lineage_edges(
        self, keys: Sequence[str], *, direction: LineageDirection, level: Level
    ) -> list[LineageEdge]:
        if direction not in ("upstream", "downstream"):
            raise ValueError(f"direction must be 'upstream' or 'downstream': {direction!r}")
        on = "downstream_key" if direction == "upstream" else "upstream_key"
        found: set[tuple[str, str, str]] = set()
        for chunk in _chunks(sorted(set(keys))):
            found.update(
                (str(r[0]), str(r[1]), str(r[2]))
                for r in self._q(
                    "SELECT downstream_key, upstream_key, fact_id FROM lineage_edges "
                    f"WHERE {on} IN ({_marks(len(chunk))}) AND level=?",
                    [*chunk, level],
                )
            )
        return [LineageEdge(up, down, level, fid) for down, up, fid in sorted(found)]

    def lineage_state_get(self, refs: Sequence[ObjectRef]) -> dict[ObjectRef, str]:
        by_key: dict[str, list[ObjectRef]] = {}
        for r in refs:
            by_key.setdefault(r.table_ref().canonical(), []).append(r)
        out: dict[ObjectRef, str] = {}
        for chunk in _chunks(sorted(by_key)):
            for key, sha in self._q(
                "SELECT table_ref, definition_sha256 FROM lineage_state "
                f"WHERE table_ref IN ({_marks(len(chunk))})",
                chunk,
            ):
                for r in by_key[key]:
                    out[r] = str(sha)
        return out

    def lineage_state_set(self, items: Mapping[ObjectRef, str | None]) -> None:
        rows = sorted((r.table_ref().canonical(), sha) for r, sha in items.items())
        with self.transaction():
            self._conn.executemany(
                "DELETE FROM lineage_state WHERE table_ref=?",
                [(key,) for key, sha in rows if sha is None],
            )
            self._conn.executemany(
                "INSERT INTO lineage_state(table_ref, definition_sha256) VALUES (?, ?) "
                "ON CONFLICT(table_ref) DO UPDATE SET definition_sha256=excluded.definition_sha256",
                [(key, sha) for key, sha in rows if sha is not None],
            )

    def iter_facts(self) -> Iterator[Fact]:
        last = ""
        while True:
            rows = self._q("SELECT id, data FROM facts WHERE id > ? ORDER BY id LIMIT ?",
                           (last, _PAGE))  # fmt: skip
            if not rows:
                return
            for _, data in rows:
                yield Fact.from_dict(json.loads(data))
            last = rows[-1][0]

    def facts_for(
        self,
        subjects: Iterable[ObjectRef | JoinRef] | None,
        kinds: Iterable[str] | None,
    ) -> list[Fact]:
        kind_list = None if kinds is None else sorted(set(kinds))
        if kind_list is not None and not kind_list:
            return []
        out: dict[str, str] = {}
        if subjects is not None:
            keys = sorted({_subject_key(s) for s in subjects})
            kind_sql = "" if kind_list is None else f" AND kind IN ({_marks(len(kind_list))})"
            for chunk in _chunks(keys):
                for fid, data in self._q(
                    f"SELECT id, data FROM facts WHERE subject_key IN ({_marks(len(chunk))})"
                    + kind_sql,
                    [*chunk, *(kind_list or ())],
                ):
                    out[fid] = data
        elif kind_list is None:
            return list(self.iter_facts())
        else:
            for chunk in _chunks(kind_list):
                for fid, data in self._q(
                    f"SELECT id, data FROM facts WHERE kind IN ({_marks(len(chunk))})", chunk
                ):
                    out[fid] = data
        return [Fact.from_dict(json.loads(out[i])) for i in sorted(out)]

    def tables_named(self, names: Iterable[str], *, limit: int) -> list[ObjectRef]:
        """Live tables whose bare table name equals one of ``names`` (case-insensitive).

        Ordered by canonical ref, at most ``limit``. Used by retrieval to honour identifiers an
        agent types verbatim ("orders") without suffix-matching in :meth:`resolve`.
        """
        wanted = sorted(set(names))
        if not wanted or limit <= 0:
            return []
        found: set[str] = set()
        for chunk in _chunks(wanted):
            for (ref,) in self._q(
                f"SELECT ref FROM tables WHERE name_lower IN "
                f"({','.join('lower(?)' for _ in chunk)}) AND withdrawn_at IS NULL "
                "ORDER BY ref LIMIT ?",
                (*chunk, limit),
            ):
                found.add(ref)
        return [_table_from_key(r) for r in sorted(found)[:limit]]

    # ----- embeddings (extra ``embeddings``) ---------------------------------------------

    def embeddings_put(
        self, model: str, rows: Iterable[tuple[ObjectRef, str, Sequence[float]]]
    ) -> None:
        """Upsert one vector per ``(model, table)`` as little-endian float32."""
        params = []
        for ref, doc_hash, vec in rows:
            if not vec:
                raise ValueError(f"empty embedding vector for {ref.canonical()}")
            params.append(
                (
                    model,
                    ref.table_ref().canonical(),
                    doc_hash,
                    len(vec),
                    struct.pack(f"<{len(vec)}f", *vec),
                )
            )
        with self.transaction():
            self._conn.executemany(
                "INSERT INTO table_embeddings(model, table_ref, doc_hash, dim, vector) "
                "VALUES (?,?,?,?,?) ON CONFLICT(model, table_ref) DO UPDATE SET "
                "doc_hash=excluded.doc_hash, dim=excluded.dim, vector=excluded.vector",
                params,
            )

    def embeddings_get(self, model: str) -> list[tuple[ObjectRef, str, int, bytes]]:
        """``(table, doc_hash, dim, float32 blob)`` for live tables, ordered by ref."""
        rows = self._q(
            "SELECT e.table_ref, e.doc_hash, e.dim, e.vector FROM table_embeddings e "
            "JOIN tables t ON t.ref = e.table_ref "
            "WHERE e.model=? AND t.withdrawn_at IS NULL ORDER BY e.table_ref",
            (model,),
        )
        return [(_table_from_key(r), str(h), int(d), bytes(v)) for r, h, d, v in rows]

    def embeddings_hashes(self, model: str) -> dict[ObjectRef, str]:
        """Every stored ``doc_hash`` for ``model`` (withdrawn tables included)."""
        rows = self._q(
            "SELECT table_ref, doc_hash FROM table_embeddings WHERE model=? ORDER BY table_ref",
            (model,),
        )
        return {_table_from_key(r): str(h) for r, h in rows}

    # ----- search ------------------------------------------------------------------------

    def _reindex(self, key: str) -> None:
        """Rebuild the search document of one table (caller holds the write transaction)."""
        conn = self._conn
        row = conn.execute("SELECT id, data FROM tables WHERE ref=?", (key,)).fetchone()
        if row is None:
            return
        rowid, data = row
        t = json.loads(data)
        ref = ObjectRef.from_dict(t["ref"])
        col_tokens: list[str] = []
        text: list[str] = [*name_tokens(ref.schema or ""), t.get("comment") or ""]
        for (cdata,) in conn.execute(
            "SELECT data FROM columns WHERE table_ref=? ORDER BY ordinal", (key,)
        ):
            c = json.loads(cdata)
            col_tokens.extend(name_tokens(c["ref"]["column"]))
            if c.get("comment"):
                text.append(c["comment"])
        # table-level and column-level meaning facts (column keys extend the table key by ".col")
        for (fdata,) in conn.execute(
            "SELECT data FROM facts WHERE subject_key >= ? AND subject_key < ? "
            f"AND kind IN ({_marks(len(MEANING_KINDS))})",
            (key, key + "/", *MEANING_KINDS),
        ):
            fact = json.loads(fdata)
            subj = fact["subject"]
            if "table" in subj and ObjectRef.from_dict(subj).table_ref().canonical() == key:
                text.extend(strings(fact["value"]))
        # glossary terms that map to this table (prefiltered on the table name's JSON text)
        for (fdata,) in conn.execute(
            "SELECT data FROM facts WHERE kind=? AND instr(data, ?) > 0 ORDER BY id",
            (GLOSSARY, _dumps(ref.table)[1:-1]),
        ):
            fact = json.loads(fdata)
            term = fact["value"].get("term")
            if isinstance(term, str) and key in glossary_tables(fact["value"]):
                text.append(term)
        name = " ".join(name_tokens(ref.table))
        cols = " ".join(col_tokens)
        body = " ".join(x for x in text if x)
        if self._fts:
            conn.execute("DELETE FROM table_docs WHERE rowid=?", (rowid,))
            conn.execute(
                "INSERT INTO table_docs(rowid, ref, name_tokens, columns, text) VALUES (?,?,?,?,?)",
                (rowid, key, name, cols, body),
            )
        else:
            conn.execute(
                "INSERT INTO docs(ref, name_tokens, columns, text) VALUES (?,?,?,?) "
                "ON CONFLICT(ref) DO UPDATE SET name_tokens=excluded.name_tokens, "
                "columns=excluded.columns, text=excluded.text",
                (key, name, cols, body),
            )

    def search(self, query: str, k: int) -> list[tuple[ObjectRef, float]]:
        tokens = list(dict.fromkeys(name_tokens(query)))
        if not tokens or k <= 0:
            return []
        if self._fts:
            match = " OR ".join(f'"{t}"*' if len(t) >= 3 else f'"{t}"' for t in tokens)
            rows = self._q(
                "SELECT t.ref, -bm25(table_docs, " + _FTS_WEIGHTS + ") AS score "
                "FROM table_docs JOIN tables t ON t.id = table_docs.rowid "
                "WHERE table_docs MATCH ? AND t.withdrawn_at IS NULL "
                "ORDER BY score DESC, t.ref LIMIT ?",
                (match, k),
            )
        else:
            parts = []
            params: list[Any] = []
            for t in tokens:
                p = _like(t)
                parts.append(
                    "(CASE WHEN d.name_tokens LIKE ? ESCAPE '\\' THEN 10 ELSE 0 END"
                    " + CASE WHEN d.columns LIKE ? ESCAPE '\\' THEN 3 ELSE 0 END"
                    " + CASE WHEN d.text LIKE ? ESCAPE '\\' THEN 1 ELSE 0 END)"
                )
                params.extend([p, p, p])
            rows = self._q(
                f"SELECT * FROM (SELECT d.ref AS ref, {' + '.join(parts)} AS score FROM docs d "
                "JOIN tables t ON t.ref = d.ref WHERE t.withdrawn_at IS NULL) "
                "WHERE score > 0 ORDER BY score DESC, ref LIMIT ?",
                (*params, k),
            )
        return [(_table_from_key(r[0]), float(r[1])) for r in rows]

    # ----- snapshots ---------------------------------------------------------------------

    def snapshot(self, label: str | None = None, *, checks_run: Iterable[str] = ()) -> str:
        with self.transaction():
            cur = self._conn.execute(
                "INSERT INTO snapshots(id, label, created_at, checks_run) VALUES ('', ?, ?, ?)",
                (label, to_iso(utcnow()), _dumps(sorted(set(checks_run)))),
            )
            seq = cur.lastrowid
            sid = f"snap-{seq:06d}"
            self._conn.execute("UPDATE snapshots SET id=? WHERE seq=?", (sid, seq))
            # the live rows' bytes, unchanged (the snapshot id is the row's own column), so a
            # snapshot of the same facts is byte-identical whatever order they were written in
            self._conn.execute(
                "INSERT INTO snapshot_facts(snapshot_id, fact_id, data) "
                "SELECT ?, id, data FROM facts",
                (sid,),
            )
            return sid

    def snapshot_facts(self, snapshot_id: str) -> list[Fact]:
        rows = self._q(
            "SELECT data FROM snapshot_facts WHERE snapshot_id=? ORDER BY fact_id", (snapshot_id,)
        )
        return [Fact.from_dict({**json.loads(r[0]), "snapshot_id": snapshot_id}) for r in rows]

    def snapshot_checks_run(self, snapshot_id: str) -> frozenset[str]:
        rows = self._q("SELECT checks_run FROM snapshots WHERE id=?", (snapshot_id,))
        return frozenset(json.loads(rows[0][0])) if rows else frozenset()

    def latest_snapshot(self) -> str | None:
        rows = self._q("SELECT id FROM snapshots ORDER BY seq DESC LIMIT 1")
        return str(rows[0][0]) if rows else None

    # ----- state, ledger, queue, feedback ------------------------------------------------

    def crawl_state_get(self, key: str) -> Mapping[str, object] | None:
        rows = self._q("SELECT value FROM crawl_state WHERE key=?", (key,))
        return None if not rows else dict(json.loads(rows[0][0]))

    def crawl_state_set(self, key: str, value: Mapping[str, object]) -> None:
        with self.transaction():
            self._conn.execute(
                "INSERT INTO crawl_state(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, _dumps(dict(value))),
            )

    def ledger_add(self, kind: str, amount: int, unit: LedgerUnit, note: str) -> None:
        with self.transaction():
            self._conn.execute(
                "INSERT INTO ledger(kind, unit, amount, note, at) VALUES (?,?,?,?,?)",
                (kind, unit, amount, note, to_iso(utcnow())),
            )

    def ledger_total(self, kind: str, unit: LedgerUnit, since: datetime | None) -> int:
        sql = "SELECT COALESCE(SUM(amount), 0) FROM ledger WHERE kind=? AND unit=?"
        params: list[Any] = [kind, unit]
        if since is not None:
            sql += " AND at >= ?"
            params.append(to_iso(since))
        return int(self._q(sql, params)[0][0])

    def queue_push(self, table: ObjectRef, reason: str, priority: float) -> None:
        with self.transaction():
            self._conn.execute(
                "INSERT INTO queue(table_ref, reason, priority, seq) VALUES "
                "(?, ?, ?, (SELECT COALESCE(MAX(seq), 0) + 1 FROM queue)) "
                "ON CONFLICT(table_ref, reason) DO UPDATE SET "
                "priority=MAX(priority, excluded.priority)",
                (table.table_ref().canonical(), reason, priority),
            )

    def queue_pop(self, n: int) -> list[tuple[ObjectRef, str]]:
        with self.transaction():
            rows = self._conn.execute(
                "SELECT table_ref, reason FROM queue ORDER BY priority DESC, seq LIMIT ?", (n,)
            ).fetchall()
            self._conn.executemany(
                "DELETE FROM queue WHERE table_ref=? AND reason=?", [(r[0], r[1]) for r in rows]
            )
        return [(_table_from_key(r[0]), r[1]) for r in rows]

    def feedback_add(self, table: ObjectRef, column: str | None, failure_class: str) -> None:
        with self.transaction():
            self._conn.execute(
                "INSERT INTO feedback(table_ref, column_name, failure_class, at) VALUES (?,?,?,?)",
                (table.table_ref().canonical(), column, failure_class, to_iso(utcnow())),
            )

    def feedback_summary(self) -> list[dict[str, object]]:
        rows = self._q(
            "SELECT table_ref, column_name, failure_class, COUNT(*) FROM feedback "
            "GROUP BY table_ref, column_name, failure_class "
            "ORDER BY COUNT(*) DESC, table_ref, column_name, failure_class"
        )
        return [{"table": r[0], "column": r[1], "failure_class": r[2], "count": r[3]} for r in rows]
