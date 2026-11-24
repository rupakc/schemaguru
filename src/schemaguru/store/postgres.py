"""``PostgresStore``: the shared store for teams and server mode (spec 7.2, 17; extra
``postgres-store``).

Same model and contract as :class:`~schemaguru.store.sqlite.SQLiteStore`, so several processes
(server replicas, workers, CLI runs) can share one store:

* **Connections**: a small bounded pool of psycopg 3 connections in autocommit mode. A
  :meth:`transaction` pins one connection to the calling thread; every write runs in one, and
  nested transactions join the outer one (as in SQLite).
* **Migrations**: their own numbered series (``store/migrations/pg/``), applied on open inside
  a transaction holding ``pg_advisory_xact_lock``, so replicas starting together migrate once.
  A newer schema version is refused (``SG702``).
* **Search**: one weighted ``tsvector`` per table (name tokens A, column tokens B, comments and
  descriptions C) built from the same documents SQLite indexes. Each query token is a prefix
  match (3+ characters) against both ``simple`` and ``english`` (stemmed) lexemes. Score per
  token: 10 for a table-name hit, 3 for a column hit, 1 for a text hit — SQLite's LIKE-fallback
  weights — plus ``ts_rank`` (< 1) as a tie-break. Name hits therefore always rank first, as in
  SQLite; *within* a field the order can differ from SQLite FTS5's bm25 (no document-length
  normalisation here), and ``english`` drops stop words from comment text.
* **Snapshots** copy the facts table set-based (``INSERT … SELECT``).
* **Queue**: ``queue_pop`` claims rows with ``FOR UPDATE SKIP LOCKED``, so concurrent workers
  never pop the same entry.
* **Embeddings** are stored as the same little-endian float32 bytes as SQLite (``BYTEA``);
  similarity is computed by the retriever. pgvector is not required.
* :meth:`advisory_lock` (``pg_try_advisory_lock`` on a dedicated connection) lets the server
  run one refresh at a time across replicas.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from schemaguru._util.hashing import canonical_json
from schemaguru._util.text import name_tokens
from schemaguru._util.timeutil import to_iso, utcnow
from schemaguru.diagnostics import DiagnosticBag
from schemaguru.errors import (
    SchemaGuruError,
    StoreConfigError,
    StoreUnavailableError,
    StoreVersionError,
)
from schemaguru.model.facts import Fact
from schemaguru.model.joins import JoinRef
from schemaguru.model.refs import ObjectRef
from schemaguru.model.structure import Column, Constraint, Schema, Table
from schemaguru.store import schema_sql
from schemaguru.store.protocol import LedgerUnit

log = logging.getLogger("schemaguru.store")

_PAGE = 1000
_MEANING_KINDS = ("DESCRIPTION", "USE_CASES", "ENTITY_TYPE", "PITFALL")
_SCHEMA_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}\Z")
_RANK_WEIGHTS = "{0.0, 0.1, 0.3, 1.0}"  # D, C (text), B (columns), A (name)

_DOC_SQL = (
    "setweight(to_tsvector('simple', %(n)s) || to_tsvector('english', %(n)s), 'A') || "
    "setweight(to_tsvector('simple', %(c)s) || to_tsvector('english', %(c)s), 'B') || "
    "setweight(to_tsvector('simple', %(t)s) || to_tsvector('english', %(t)s), 'C')"
)

_dumps = canonical_json


def _subject_key(subject: ObjectRef | JoinRef) -> str:
    return subject.identity_key() if isinstance(subject, JoinRef) else subject.canonical()


def _table_from_key(key: str) -> ObjectRef:
    return ObjectRef.parse(key, has_column=False)


def _strings(obj: Any) -> Iterator[str]:
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, Mapping):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _strings(v)


def _row_to_table(data: str, withdrawn_at: str | None) -> Table:
    d = json.loads(data)
    d["withdrawn_at"] = None
    if withdrawn_at is not None:
        d["withdrawn_at"] = (
            datetime.strptime(withdrawn_at, "%Y-%m-%dT%H:%M:%S.%fZ")
            .replace(tzinfo=timezone.utc)
            .isoformat()
        )
    return Table.from_dict(d)


def _lexeme(token: str) -> str:
    """A tsquery operand: quoted, prefix-matching for tokens of 3+ characters (as SQLite)."""
    quoted = "'" + token.replace("\\", "\\\\").replace("'", "''") + "'"
    return quoted + ":*" if len(token) >= 3 else quoted


# An advisory-lock key computed by the server from what identifies the store there: the database
# and the schema (the named one, else the connection's current schema). Advisory locks are
# per database, so every replica agrees on the key however its DSN is spelled.
_LOCK_KEY_SQL = (
    "hashtextextended(%(purpose)s || chr(31) || current_database() || chr(31) || "
    "COALESCE(%(schema)s, current_schema()) || chr(31) || %(name)s, 0)"
)


@contextmanager
def _unavailable(where: str) -> Iterator[None]:
    """Turn a lost or refused connection into ``StoreUnavailableError`` (``SG703``)."""
    import psycopg

    try:
        yield
    except psycopg.OperationalError as e:
        raise StoreUnavailableError(
            f"SG703: the Postgres store at {where} is unavailable: {type(e).__name__}"
        ) from None


# SQLSTATE invalid_password / invalid_authorization_specification
_AUTH_STATES = frozenset({"28P01", "28000"})


def _auth_failed(e: Exception) -> bool:
    """Did the server reject the credentials (rather than not answer)?"""
    if getattr(e, "sqlstate", None) in _AUTH_STATES:
        return True
    text = str(e).lower()
    return "authentication failed" in text or "no password supplied" in text


def _where(dsn: str) -> str:
    """Host/port/database of a DSN for messages; never the password."""
    try:
        from psycopg.conninfo import conninfo_to_dict

        info = conninfo_to_dict(dsn)
    except Exception:
        return "<unparseable DSN>"
    return f"{info.get('host') or 'localhost'}:{info.get('port') or 5432}/{info.get('dbname', '')}"


class _Pool:
    """A bounded set of autocommit connections; broken or mid-transaction ones are discarded."""

    def __init__(
        self, connect: Callable[[], Any], max_size: int, timeout_s: float, where: str
    ) -> None:
        self._connect = connect
        self._timeout_s = timeout_s
        self._where = where
        self._sem = threading.BoundedSemaphore(max_size)
        self._max = max_size
        self._lock = threading.Lock()
        self._free: list[Any] = []
        self._closed = False

    def acquire(self) -> Any:
        if not self._sem.acquire(timeout=self._timeout_s):
            raise StoreUnavailableError(
                f"SG703: no Postgres store connection free at {self._where} within "
                f"{self._timeout_s:g} s (all {self._max} in use)"
            )
        try:
            with self._lock:
                if self._closed:
                    raise SchemaGuruError("the Postgres store is closed")
                while self._free:
                    conn = self._free.pop()
                    if not conn.closed and not conn.broken:
                        return conn
            return self._connect()
        except BaseException:
            self._sem.release()
            raise

    def release(self, conn: Any) -> None:
        try:
            from psycopg.pq import TransactionStatus

            idle = conn.info.transaction_status == TransactionStatus.IDLE
            with self._lock:
                if self._closed or conn.closed or conn.broken or not idle:
                    conn.close()
                else:
                    self._free.append(conn)
        finally:
            self._sem.release()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            free, self._free = self._free, []
        for conn in free:
            conn.close()


class PostgresStore:
    """A :class:`~schemaguru.store.protocol.Store` in a Postgres database (psycopg 3).

    ``schema`` puts every table in that Postgres schema (created if missing) instead of the
    connection's default ``search_path``; several stores can then share one database.
    """

    def __init__(
        self,
        dsn: str,
        *,
        schema: str | None = None,
        max_connections: int = 8,
        acquire_timeout_s: float = 30.0,
        connect_timeout_s: int = 10,
    ) -> None:
        if schema is not None and not _SCHEMA_NAME.match(schema):
            raise ValueError(f"invalid Postgres schema name {schema!r}")
        if max_connections < 1:
            raise ValueError("max_connections must be at least 1")
        if acquire_timeout_s <= 0 or connect_timeout_s <= 0:
            raise ValueError("timeouts must be positive")
        self._connect_timeout_s = connect_timeout_s
        try:
            import psycopg  # noqa: F401
        except ImportError:
            raise ImportError(
                "PostgresStore needs psycopg 3; install it with: "
                "pip install 'schemaguru[postgres-store]'"
            ) from None
        self.dsn = dsn
        self.schema = schema
        self.path = f"postgres://{_where(dsn)}" + (f" schema {schema}" if schema else "")
        self.diagnostics = DiagnosticBag()
        self._local = threading.local()
        self._pool = _Pool(self._new_connection, max_connections, acquire_timeout_s, _where(dsn))
        try:
            self._migrate()
        except BaseException:
            self._pool.close()
            raise

    # ----- lifecycle ---------------------------------------------------------------------

    def _new_connection(self) -> Any:
        import psycopg
        from psycopg import sql

        try:
            conn = psycopg.connect(
                self.dsn, autocommit=True, connect_timeout=self._connect_timeout_s
            )
        except psycopg.OperationalError as e:
            if _auth_failed(e):  # the DSN's user/password: configuration, not an outage
                raise StoreConfigError(
                    f"SG704: the Postgres store at {_where(self.dsn)} rejected the configured "
                    "credentials (authentication failed); check the DSN's user and password"
                ) from None
            raise StoreUnavailableError(
                f"SG703: cannot connect to the Postgres store at {_where(self.dsn)}: "
                f"{str(e).strip()}"
            ) from None
        if self.schema is not None:
            with _unavailable(_where(self.dsn)):
                conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(self.schema)))
        return conn

    def _key_params(self, purpose: str, name: str = "") -> dict[str, Any]:
        return {"purpose": purpose, "schema": self.schema, "name": name}

    def close(self) -> None:
        self._pool.close()

    def __enter__(self) -> PostgresStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _migrate(self) -> None:
        from psycopg import sql

        with self.transaction():
            conn = self._local.conn
            conn.execute(
                f"SELECT pg_advisory_xact_lock({_LOCK_KEY_SQL})",
                self._key_params("schemaguru-migrate"),
            )
            if self.schema is not None:
                conn.execute(
                    sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(self.schema))
                )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            current = int(
                conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0]
            )
            if current > schema_sql.LATEST_PG_VERSION:
                raise StoreVersionError(
                    f"SG702: Postgres store {self.path!r} has schema version {current}, but "
                    f"this SchemaGuru supports up to {schema_sql.LATEST_PG_VERSION}; upgrade "
                    "SchemaGuru or use a different schema"
                )
            for version, script in schema_sql.migrations("pg"):
                if version <= current:
                    continue
                conn.execute(script)
                conn.execute(
                    "INSERT INTO schema_version(version, applied_at) VALUES (%s, %s)",
                    (version, to_iso(utcnow())),
                )

    def schema_version(self) -> int:
        return int(self._q("SELECT COALESCE(MAX(version), 0) FROM schema_version")[0][0])

    # ----- connections and transactions ----------------------------------------------------

    @contextmanager
    def _conn(self) -> Iterator[Any]:
        """The thread's transaction connection, or a pooled one for a single statement."""
        pinned = getattr(self._local, "conn", None)
        if pinned is not None:
            yield pinned
            return
        conn = self._pool.acquire()
        try:
            with _unavailable(_where(self.dsn)):
                yield conn
        finally:
            self._pool.release(conn)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        depth = getattr(self._local, "depth", 0)
        if depth:
            self._local.depth = depth + 1
            try:
                yield
            finally:
                self._local.depth -= 1
            return
        conn = self._pool.acquire()
        self._local.conn, self._local.depth = conn, 1
        try:
            with _unavailable(_where(self.dsn)), conn.transaction():
                yield
        finally:
            self._local.conn, self._local.depth = None, 0
            self._pool.release(conn)

    def _q(self, query: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> list[Any]:
        with self._conn() as conn:
            return list(conn.execute(query, params).fetchall())

    def _x(self, query: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> None:
        with self._conn() as conn:
            conn.execute(query, params)

    def _many(self, query: str, rows: Sequence[Sequence[Any] | Mapping[str, Any]]) -> None:
        if not rows:
            return
        with self._conn() as conn, conn.cursor() as cur:
            cur.executemany(query, rows)

    @contextmanager
    def advisory_lock(self, name: str) -> Iterator[bool]:
        """Try a session-level ``pg_try_advisory_lock`` for ``name`` in this store's namespace.

        Yields whether it was acquired (never waits). The lock lives on a dedicated connection
        for the duration of the block, so it is released even if that connection dies.
        """
        conn = self._new_connection()
        try:
            with _unavailable(_where(self.dsn)):
                key = int(
                    conn.execute(
                        f"SELECT {_LOCK_KEY_SQL}", self._key_params("schemaguru-lock", name)
                    ).fetchone()[0]
                )
                got = bool(conn.execute("SELECT pg_try_advisory_lock(%s)", (key,)).fetchone()[0])
            try:
                yield got
            finally:
                if got and not conn.closed and not conn.broken:
                    conn.execute("SELECT pg_advisory_unlock(%s)", (key,))
        finally:
            conn.close()

    # ----- structure ---------------------------------------------------------------------

    def upsert_schemas(self, items: Iterable[Schema]) -> None:
        with self.transaction():
            self._many(
                "INSERT INTO schemas(key, data) VALUES (%s, %s) "
                "ON CONFLICT(key) DO UPDATE SET data=excluded.data",
                [(s.key(), _dumps(s.to_dict())) for s in items],
            )

    def iter_schemas(self) -> Iterator[Schema]:
        for (data,) in self._q("SELECT data FROM schemas ORDER BY key"):
            yield Schema.from_dict(json.loads(data))

    def upsert_tables(self, items: Iterable[Table]) -> None:
        """One ``executemany`` and one reindex pass per page of ``_PAGE`` tables."""
        with self.transaction():
            page: dict[str, tuple[str, str, str, str, str | None]] = {}
            for t in items:
                d = t.to_dict()
                del d["withdrawn_at"]
                key = t.ref.canonical()
                page[key] = (
                    key,
                    key.lower(),
                    t.ref.table.lower(),
                    _dumps(d),
                    None if t.withdrawn_at is None else to_iso(t.withdrawn_at),
                )
                if len(page) >= _PAGE:
                    self._upsert_table_page(page)
                    page = {}
            self._upsert_table_page(page)

    def _upsert_table_page(self, page: Mapping[str, tuple[Any, ...]]) -> None:
        if not page:
            return
        self._many(
            "INSERT INTO tables(ref, ref_lower, name_lower, data, withdrawn_at) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT(ref) DO UPDATE SET data=excluded.data, "
            "withdrawn_at=excluded.withdrawn_at",
            list(page.values()),
        )
        self._reindex_many(sorted(page))

    def table(self, ref: ObjectRef) -> Table | None:
        rows = self._q(
            "SELECT data, withdrawn_at FROM tables WHERE ref=%s", (ref.table_ref().canonical(),)
        )
        return _row_to_table(*rows[0]) if rows else None

    def iter_tables(self, *, include_withdrawn: bool = False) -> Iterator[Table]:
        last = ""
        where = "" if include_withdrawn else "AND withdrawn_at IS NULL"
        while True:
            rows = self._q(
                f"SELECT ref, data, withdrawn_at FROM tables WHERE ref > %s {where} "
                "ORDER BY ref LIMIT %s",
                (last, _PAGE),
            )
            if not rows:
                return
            for _ref, data, wat in rows:
                yield _row_to_table(data, wat)
            last = rows[-1][0]

    def upsert_columns(self, table: ObjectRef, cols: Sequence[Column]) -> None:
        key = table.table_ref().canonical()
        with self.transaction():
            self._x("DELETE FROM columns WHERE table_ref=%s", (key,))
            self._many(
                "INSERT INTO columns(table_ref, name, ordinal, data) VALUES (%s,%s,%s,%s) "
                "ON CONFLICT(table_ref, name) DO UPDATE SET ordinal=excluded.ordinal, "
                "data=excluded.data",
                [(key, c.ref.column, c.ordinal, _dumps(c.to_dict())) for c in cols],
            )
            self._reindex(key)

    def columns(self, ref: ObjectRef) -> list[Column]:
        rows = self._q(
            "SELECT data FROM columns WHERE table_ref=%s ORDER BY ordinal, name",
            (ref.table_ref().canonical(),),
        )
        return [Column.from_dict(json.loads(r[0])) for r in rows]

    def upsert_constraints(self, table: ObjectRef, cons: Sequence[Constraint]) -> None:
        key = table.table_ref().canonical()
        with self.transaction():
            self._x("DELETE FROM constraints WHERE table_ref=%s", (key,))
            self._many(
                "INSERT INTO constraints(table_ref, seq, data) VALUES (%s,%s,%s)",
                [(key, i, _dumps(c.to_dict())) for i, c in enumerate(cons)],
            )

    def constraints(self, ref: ObjectRef) -> list[Constraint]:
        rows = self._q(
            "SELECT data FROM constraints WHERE table_ref=%s ORDER BY seq",
            (ref.table_ref().canonical(),),
        )
        return [Constraint.from_dict(json.loads(r[0])) for r in rows]

    def mark_withdrawn(self, refs: Iterable[ObjectRef], at: datetime) -> None:
        stamp = to_iso(at)  # rejects naive datetimes
        with self.transaction():
            self._many(
                "UPDATE tables SET withdrawn_at=COALESCE(withdrawn_at, %s) WHERE ref=%s",
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
        if self._q("SELECT 1 FROM tables WHERE ref=%s", (key,)):
            return ref, []
        rows = self._q("SELECT ref FROM tables WHERE ref_lower=%s ORDER BY ref", (key.lower(),))
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
                "SELECT name FROM columns WHERE table_ref=%s ORDER BY name", (table.canonical(),)
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
            rows: dict[str, tuple[str, str, str, str]] = {}
            for f in facts:
                rows[f.id] = (f.id, f.kind.name, _subject_key(f.subject), _dumps(f.to_dict()))
                if f.kind.name in _MEANING_KINDS and isinstance(f.subject, ObjectRef):
                    touched.add(f.subject.table_ref().canonical())
            self._many(
                "INSERT INTO facts(id, kind, subject_key, data) VALUES (%s,%s,%s,%s) "
                "ON CONFLICT(id) DO UPDATE SET kind=excluded.kind, "
                "subject_key=excluded.subject_key, data=excluded.data",
                list(rows.values()),
            )
            self._reindex_many(sorted(touched))

    def delete_facts(self, ids: Iterable[str]) -> int:
        wanted = sorted(set(ids))
        if not wanted:
            return 0
        with self.transaction():
            touched: set[str] = set()
            rows = self._q("SELECT id, data FROM facts WHERE id = ANY(%s)", (wanted,))
            for _, data in rows:
                f = Fact.from_dict(json.loads(data))
                if f.kind.name in _MEANING_KINDS and isinstance(f.subject, ObjectRef):
                    touched.add(f.subject.table_ref().canonical())
            self._x("DELETE FROM facts WHERE id = ANY(%s)", (wanted,))
            self._reindex_many(sorted(touched))
        return len(rows)

    def iter_facts(self) -> Iterator[Fact]:
        last = ""
        while True:
            rows = self._q(
                "SELECT id, data FROM facts WHERE id > %s ORDER BY id LIMIT %s", (last, _PAGE)
            )
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
        if subjects is None and kind_list is None:
            return list(self.iter_facts())
        where: list[str] = []
        params: list[Any] = []
        if subjects is not None:
            keys = sorted({_subject_key(s) for s in subjects})
            if not keys:
                return []
            where.append("subject_key = ANY(%s)")
            params.append(keys)
        if kind_list is not None:
            where.append("kind = ANY(%s)")
            params.append(kind_list)
        rows = self._q(f"SELECT data FROM facts WHERE {' AND '.join(where)} ORDER BY id", params)
        return [Fact.from_dict(json.loads(r[0])) for r in rows]

    def tables_named(self, names: Iterable[str], *, limit: int) -> list[ObjectRef]:
        wanted = sorted({n.lower() for n in names})
        if not wanted or limit <= 0:
            return []
        rows = self._q(
            "SELECT ref FROM tables WHERE name_lower = ANY(%s) AND withdrawn_at IS NULL "
            "ORDER BY ref LIMIT %s",
            (wanted, limit),
        )
        return [_table_from_key(r[0]) for r in rows]

    # ----- embeddings (extra ``embeddings``) ---------------------------------------------

    def embeddings_put(
        self, model: str, rows: Iterable[tuple[ObjectRef, str, Sequence[float]]]
    ) -> None:
        """Upsert one vector per ``(model, table)`` as little-endian float32."""
        import struct

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
            self._many(
                "INSERT INTO table_embeddings(model, table_ref, doc_hash, dim, vector) "
                "VALUES (%s,%s,%s,%s,%s) ON CONFLICT(model, table_ref) DO UPDATE SET "
                "doc_hash=excluded.doc_hash, dim=excluded.dim, vector=excluded.vector",
                params,
            )

    def embeddings_get(self, model: str) -> list[tuple[ObjectRef, str, int, bytes]]:
        """``(table, doc_hash, dim, float32 blob)`` for live tables, ordered by ref."""
        rows = self._q(
            "SELECT e.table_ref, e.doc_hash, e.dim, e.vector FROM table_embeddings e "
            "JOIN tables t ON t.ref = e.table_ref "
            "WHERE e.model=%s AND t.withdrawn_at IS NULL ORDER BY e.table_ref",
            (model,),
        )
        return [(_table_from_key(r), str(h), int(d), bytes(v)) for r, h, d, v in rows]

    def embeddings_hashes(self, model: str) -> dict[ObjectRef, str]:
        """Every stored ``doc_hash`` for ``model`` (withdrawn tables included)."""
        rows = self._q(
            "SELECT table_ref, doc_hash FROM table_embeddings WHERE model=%s ORDER BY table_ref",
            (model,),
        )
        return {_table_from_key(r): str(h) for r, h in rows}

    # ----- search ------------------------------------------------------------------------

    def _reindex(self, key: str) -> None:
        self._reindex_many([key])

    def _reindex_many(self, keys: Sequence[str]) -> None:
        """Rebuild the search documents of ``keys`` in one pass (caller holds the transaction):
        one query each for tables, columns and meaning facts, one ``executemany`` for docs."""
        if not keys:
            return
        tables = dict(self._q("SELECT ref, data FROM tables WHERE ref = ANY(%s)", (list(keys),)))
        if not tables:
            return
        cols: dict[str, list[str]] = {}
        for ref, cdata in self._q(
            "SELECT table_ref, data FROM columns WHERE table_ref = ANY(%s) "
            "ORDER BY table_ref, ordinal",
            (list(tables),),
        ):
            cols.setdefault(ref, []).append(cdata)
        meaning: dict[str, list[str]] = {}
        # table-level and column-level meaning facts (column keys extend the table key by ".col")
        for key, fdata in self._q(
            "SELECT k.key, f.data FROM unnest(%s::text[]) AS k(key) JOIN facts f "
            "ON f.subject_key >= k.key COLLATE \"C\" AND f.subject_key < (k.key || '/') "
            'COLLATE "C" WHERE f.kind = ANY(%s) ORDER BY k.key, f.id',
            (list(tables), list(_MEANING_KINDS)),
        ):
            meaning.setdefault(key, []).append(fdata)
        docs = []
        for key in sorted(tables):
            t = json.loads(tables[key])
            ref = ObjectRef.from_dict(t["ref"])
            col_tokens: list[str] = []
            text: list[str] = [*name_tokens(ref.schema or ""), t.get("comment") or ""]
            for cdata in cols.get(key, []):
                c = json.loads(cdata)
                col_tokens.extend(name_tokens(c["ref"]["column"]))
                if c.get("comment"):
                    text.append(c["comment"])
            for fdata in meaning.get(key, []):
                fact = json.loads(fdata)
                subj = fact["subject"]
                if "table" in subj and ObjectRef.from_dict(subj).table_ref().canonical() == key:
                    text.extend(_strings(fact["value"]))
            docs.append(
                {
                    "r": key,
                    "n": " ".join(name_tokens(ref.table)),
                    "c": " ".join(col_tokens),
                    "t": " ".join(x for x in text if x),
                }
            )
        self._many(
            "INSERT INTO docs(ref, name_tokens, columns, text, doc) "
            f"VALUES (%(r)s, %(n)s, %(c)s, %(t)s, {_DOC_SQL}) "
            "ON CONFLICT(ref) DO UPDATE SET name_tokens=excluded.name_tokens, "
            "columns=excluded.columns, text=excluded.text, doc=excluded.doc",
            docs,
        )

    def search(self, query: str, k: int) -> list[tuple[ObjectRef, float]]:
        tokens = list(dict.fromkeys(name_tokens(query)))
        if not tokens or k <= 0:
            return []
        params: dict[str, Any] = {"k": k, "w": _RANK_WEIGHTS}
        per_token: list[str] = []
        for i, tok in enumerate(tokens):
            params[f"q{i}"] = _lexeme(tok)
            q = f"(to_tsquery('simple', %(q{i})s) || to_tsquery('english', %(q{i})s))"
            per_token.append(
                f"10 * (ts_filter(d.doc, '{{a}}') @@ {q})::int"
                f" + 3 * (ts_filter(d.doc, '{{b}}') @@ {q})::int"
                f" + (ts_filter(d.doc, '{{c}}') @@ {q})::int"
            )
        any_q = " || ".join(
            f"to_tsquery('simple', %(q{i})s) || to_tsquery('english', %(q{i})s)"
            for i in range(len(tokens))
        )
        rows = self._q(
            "SELECT ref, score FROM ("
            f"SELECT d.ref AS ref, {' + '.join(per_token)} "
            f"+ ts_rank(%(w)s::float4[], d.doc, q.q) AS score "
            f"FROM docs d JOIN tables t ON t.ref = d.ref, (SELECT {any_q} AS q) q "
            "WHERE t.withdrawn_at IS NULL AND d.doc @@ q.q"
            ") s ORDER BY score DESC, ref LIMIT %(k)s",
            params,
        )
        return [(_table_from_key(r[0]), float(r[1])) for r in rows]

    # ----- snapshots ---------------------------------------------------------------------

    def snapshot(self, label: str | None = None, *, checks_run: Iterable[str] = ()) -> str:
        with self.transaction():
            seq = int(self._q("SELECT nextval(pg_get_serial_sequence('snapshots', 'seq'))")[0][0])
            sid = f"snap-{seq:06d}"
            self._x(
                "INSERT INTO snapshots(seq, id, label, created_at, checks_run) "
                "VALUES (%s, %s, %s, %s, %s)",
                (seq, sid, label, to_iso(utcnow()), _dumps(sorted(set(checks_run)))),
            )
            self._x(
                "INSERT INTO snapshot_facts(snapshot_id, fact_id, data) "
                "SELECT %s, id, jsonb_set(data::jsonb, '{snapshot_id}', to_jsonb(%s::text))::text "
                "FROM facts",
                (sid, sid),
            )
            return sid

    def snapshot_facts(self, snapshot_id: str) -> list[Fact]:
        rows = self._q(
            "SELECT data FROM snapshot_facts WHERE snapshot_id=%s ORDER BY fact_id",
            (snapshot_id,),
        )
        return [Fact.from_dict(json.loads(r[0])) for r in rows]

    def snapshot_checks_run(self, snapshot_id: str) -> frozenset[str]:
        rows = self._q("SELECT checks_run FROM snapshots WHERE id=%s", (snapshot_id,))
        return frozenset(json.loads(rows[0][0])) if rows else frozenset()

    def latest_snapshot(self) -> str | None:
        rows = self._q("SELECT id FROM snapshots ORDER BY seq DESC LIMIT 1")
        return str(rows[0][0]) if rows else None

    # ----- state, ledger, queue, feedback ------------------------------------------------

    def crawl_state_get(self, key: str) -> Mapping[str, object] | None:
        rows = self._q("SELECT value FROM crawl_state WHERE key=%s", (key,))
        return None if not rows else dict(json.loads(rows[0][0]))

    def crawl_state_set(self, key: str, value: Mapping[str, object]) -> None:
        with self.transaction():
            self._x(
                "INSERT INTO crawl_state(key, value) VALUES (%s, %s) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, _dumps(dict(value))),
            )

    def ledger_add(self, kind: str, amount: int, unit: LedgerUnit, note: str) -> None:
        with self.transaction():
            self._x(
                "INSERT INTO ledger(kind, unit, amount, note, at) VALUES (%s,%s,%s,%s,%s)",
                (kind, unit, amount, note, to_iso(utcnow())),
            )

    def ledger_total(self, kind: str, unit: LedgerUnit, since: datetime | None) -> int:
        query = "SELECT COALESCE(SUM(amount), 0) FROM ledger WHERE kind=%s AND unit=%s"
        params: list[Any] = [kind, unit]
        if since is not None:
            query += " AND at >= %s"
            params.append(to_iso(since))
        return int(self._q(query, params)[0][0])

    def queue_push(self, table: ObjectRef, reason: str, priority: float) -> None:
        with self.transaction():
            self._x(
                "INSERT INTO queue(table_ref, reason, priority) VALUES (%s, %s, %s) "
                "ON CONFLICT(table_ref, reason) DO UPDATE SET "
                "priority=GREATEST(queue.priority, excluded.priority)",
                (table.table_ref().canonical(), reason, priority),
            )

    def queue_pop(self, n: int) -> list[tuple[ObjectRef, str]]:
        if n <= 0:
            return []
        with self.transaction():
            rows = self._q(
                "DELETE FROM queue WHERE (table_ref, reason) IN ("
                "SELECT table_ref, reason FROM queue ORDER BY priority DESC, seq LIMIT %s "
                "FOR UPDATE SKIP LOCKED) RETURNING table_ref, reason, priority, seq",
                (n,),
            )
        rows.sort(key=lambda r: (-r[2], r[3]))
        return [(_table_from_key(r[0]), r[1]) for r in rows]

    def feedback_add(self, table: ObjectRef, column: str | None, failure_class: str) -> None:
        with self.transaction():
            self._x(
                "INSERT INTO feedback(table_ref, column_name, failure_class, at) "
                "VALUES (%s,%s,%s,%s)",
                (table.table_ref().canonical(), column, failure_class, to_iso(utcnow())),
            )

    def feedback_summary(self) -> list[dict[str, object]]:
        rows = self._q(
            "SELECT table_ref, column_name, failure_class, COUNT(*) FROM feedback "
            "GROUP BY table_ref, column_name, failure_class "
            "ORDER BY COUNT(*) DESC, table_ref, column_name NULLS FIRST, failure_class"
        )
        return [
            {"table": r[0], "column": r[1], "failure_class": r[2], "count": int(r[3])} for r in rows
        ]
