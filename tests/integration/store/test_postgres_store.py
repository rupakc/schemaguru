"""The ``Store`` contract on both backends side by side, plus Postgres-only behaviour.

``store`` is parametrised over SQLite and Postgres, so the integration job proves the two
backends pass the identical suite (``tests/unit/store/contract.py``). Postgres needs
``SCHEMAGURU_TEST_POSTGRES_DSN``; each test gets its own schema, dropped afterwards.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from schemaguru.errors import StoreVersionError
from schemaguru.store import schema_sql
from tests.integration.conftest import engine_dsn, require_driver
from tests.unit.store.contract import *  # noqa: F403  (the contract tests run here)
from tests.unit.store.contract import col, tbl, tref

pytestmark = pytest.mark.integration


def _pg_dsn() -> str:
    require_driver("postgres", "psycopg")
    return engine_dsn("postgres")


def _drop_schema(dsn: str, schema: str) -> None:
    import psycopg
    from psycopg import sql

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def pg_schema() -> Iterator[tuple[str, str]]:
    dsn = _pg_dsn()
    schema = f"sg_test_{uuid.uuid4().hex[:12]}"
    yield dsn, schema
    _drop_schema(dsn, schema)


@pytest.fixture(params=["sqlite", "postgres"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Any]:
    if request.param == "sqlite":
        from schemaguru.store.sqlite import SQLiteStore

        with SQLiteStore(tmp_path / "t.db") as s:
            yield s
        return
    from schemaguru.store.postgres import PostgresStore

    dsn = _pg_dsn()
    schema = f"sg_test_{uuid.uuid4().hex[:12]}"
    s = PostgresStore(dsn, schema=schema)
    try:
        yield s
    finally:
        s.close()
        _drop_schema(dsn, schema)


# ----- Postgres only ---------------------------------------------------------------------------


def test_migrations_recorded_and_reopen(pg_schema: tuple[str, str]) -> None:
    from schemaguru.store.postgres import PostgresStore

    dsn, schema = pg_schema
    PostgresStore(dsn, schema=schema).close()
    with PostgresStore(dsn, schema=schema) as s:
        assert s.schema_version() == schema_sql.LATEST_PG_VERSION


def test_newer_store_refused(pg_schema: tuple[str, str]) -> None:
    import psycopg
    from psycopg import sql

    from schemaguru.store.postgres import PostgresStore

    dsn, schema = pg_schema
    PostgresStore(dsn, schema=schema).close()
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            sql.SQL("INSERT INTO {}.schema_version(version, applied_at) VALUES (999, 'x')").format(
                sql.Identifier(schema)
            )
        )
    with pytest.raises(StoreVersionError, match="SG702"):
        PostgresStore(dsn, schema=schema)


def test_concurrent_opens_migrate_once(pg_schema: tuple[str, str]) -> None:
    """Replicas starting together: the migration runs under an advisory lock, exactly once."""
    from schemaguru.store.postgres import PostgresStore

    dsn, schema = pg_schema
    errors: list[BaseException] = []
    stores: list[Any] = []

    def open_one() -> None:
        try:
            stores.append(PostgresStore(dsn, schema=schema))
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=open_one) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    try:
        assert errors == []
        assert stores[0].schema_version() == schema_sql.LATEST_PG_VERSION
    finally:
        for s in stores:
            s.close()


def test_two_stores_share_one_schema(pg_schema: tuple[str, str]) -> None:
    """Two processes (here: two store objects) see each other's writes at once."""
    from schemaguru.store.postgres import PostgresStore

    dsn, schema = pg_schema
    with PostgresStore(dsn, schema=schema) as a, PostgresStore(dsn, schema=schema) as b:
        a.upsert_tables([tbl("orders", "customer purchases")])
        a.upsert_columns(tref("orders"), [col("orders", "id", 1)])
        assert b.table(tref("orders")) is not None
        assert [r.table for r, _ in b.search("purchases", 5)] == ["orders"]


def test_advisory_lock_exclusive(pg_schema: tuple[str, str]) -> None:
    from schemaguru.store.postgres import PostgresStore

    dsn, schema = pg_schema
    with PostgresStore(dsn, schema=schema) as a, PostgresStore(dsn, schema=schema) as b:
        with a.advisory_lock("refresh") as got_a:
            assert got_a is True
            with b.advisory_lock("refresh") as got_b:
                assert got_b is False
            with b.advisory_lock("other") as got_other:
                assert got_other is True
        with b.advisory_lock("refresh") as again:
            assert again is True


def test_queue_pop_concurrent_never_duplicates(store: Any) -> None:
    for i in range(200):
        store.queue_push(tref(f"t{i:03d}"), "new", float(i % 7))
    popped: list[Any] = []
    lock = threading.Lock()

    def work() -> None:
        while True:
            got = store.queue_pop(7)
            if not got:
                return
            with lock:
                popped.extend(got)

    threads = [threading.Thread(target=work) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(popped) == 200 and len(set(popped)) == 200


def test_bad_schema_name_rejected() -> None:
    from schemaguru.store.postgres import PostgresStore

    with pytest.raises(ValueError):
        PostgresStore("postgresql://localhost/none", schema="bad; name")


def test_server_refresh_lock_across_replicas(
    pg_schema: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two server replicas on one Postgres store: a refresh held by one is refused by the other."""
    pytest.importorskip("starlette")
    pytest.importorskip("httpx")
    from starlette.testclient import TestClient

    from schemaguru.guru import Guru
    from schemaguru.server import BearerTokens, create_app
    from schemaguru.store.postgres import PostgresStore

    dsn, schema = pg_schema
    token = "admin-token-0123456789"
    with PostgresStore(dsn, schema=schema) as a, PostgresStore(dsn, schema=schema) as b:
        guru_b = Guru(store=b)
        monkeypatch.setattr(guru_b, "refresh", lambda *, label=None: _Report())
        app_b = create_app(guru_b, authenticator=BearerTokens({token: "admin"}))
        client = TestClient(app_b)
        headers = {"Authorization": f"Bearer {token}"}
        with a.advisory_lock("refresh") as got:
            assert got
            r = client.post("/refresh", headers=headers)
            assert r.status_code == 409 and r.json()["error"]["code"] == "SG809"
        r = client.post("/refresh", headers=headers)
        assert r.status_code == 200 and r.json() == {"exit_code": 0}
        guru_b.close()


class _Report:
    def to_json(self) -> str:
        return '{"exit_code": 0}'


def test_cli_opens_postgres_store_url(
    pg_schema: tuple[str, str],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    import psycopg
    from psycopg import sql

    from schemaguru.cli import main
    from schemaguru.store.postgres import PostgresStore

    dsn, schema = pg_schema
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    url = dsn + ("&" if "?" in dsn else "?") + f"options=-csearch_path%3D{schema}"
    with PostgresStore(url) as s:
        s.upsert_tables([tbl("orders")])
    for var in [v for v in os.environ if v.startswith("SCHEMAGURU_")]:
        monkeypatch.delenv(var)  # test-harness variables are not config keys
    assert main(["--format", "json", "report", "--store", url]) == 0
    assert '"tables": 1' in capsys.readouterr().out


# ----- review round 1: lock keys, pool limits, connectivity, batching --------------------------


def _spellings(dsn: str) -> list[str]:
    """The same database, spelled three ways (URL, key/value, URL with extra parameters)."""
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    info = {k: v for k, v in conninfo_to_dict(dsn).items() if v is not None}
    kv = make_conninfo(**info)
    sep = "&" if "?" in dsn else "?"
    return [dsn, kv, dsn + sep + "application_name=sg-replica"]


def test_advisory_lock_key_ignores_dsn_spelling(pg_schema: tuple[str, str]) -> None:
    from schemaguru.store.postgres import PostgresStore

    dsn, schema = pg_schema
    a_dsn, b_dsn, c_dsn = _spellings(dsn)
    with (
        PostgresStore(a_dsn, schema=schema) as a,
        PostgresStore(b_dsn, schema=schema) as b,
        PostgresStore(c_dsn, schema=schema) as c,
        a.advisory_lock("refresh") as got_a,
        b.advisory_lock("refresh") as got_b,
        c.advisory_lock("refresh") as got_c,
    ):
        assert got_a is True and got_b is False and got_c is False


def test_advisory_lock_is_per_schema(pg_schema: tuple[str, str]) -> None:
    from schemaguru.store.postgres import PostgresStore

    dsn, schema = pg_schema
    other = schema + "_b"
    try:
        with (
            PostgresStore(dsn, schema=schema) as a,
            PostgresStore(dsn, schema=other) as b,
            a.advisory_lock("refresh") as got_a,
            b.advisory_lock("refresh") as got_b,
        ):
            assert got_a is True and got_b is True
    finally:
        _drop_schema(dsn, other)


def test_concurrent_mixed_dsn_opens_migrate_once(pg_schema: tuple[str, str]) -> None:
    from schemaguru.store.postgres import PostgresStore

    dsn, schema = pg_schema
    spellings = _spellings(dsn)
    errors: list[BaseException] = []
    stores: list[Any] = []
    start = threading.Barrier(8)

    def open_one(i: int) -> None:
        try:
            start.wait()
            stores.append(PostgresStore(spellings[i % len(spellings)], schema=schema))
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=open_one, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    try:
        assert errors == []
        assert {s.schema_version() for s in stores} == {schema_sql.LATEST_PG_VERSION}
    finally:
        for s in stores:
            s.close()


def test_pool_acquire_times_out(pg_schema: tuple[str, str]) -> None:
    from schemaguru.errors import StoreUnavailableError
    from schemaguru.store.postgres import PostgresStore

    dsn, schema = pg_schema
    with PostgresStore(dsn, schema=schema, max_connections=1, acquire_timeout_s=0.3) as s:
        held, release = threading.Event(), threading.Event()

        def hold() -> None:
            with s.transaction():
                held.set()
                release.wait(10)

        t = threading.Thread(target=hold)
        t.start()
        held.wait(10)
        caught: list[BaseException] = []

        def read() -> None:
            try:
                s.table(tref("orders"))
            except BaseException as e:
                caught.append(e)

        r = threading.Thread(target=read)
        r.start()
        r.join()
        release.set()
        t.join()
        assert len(caught) == 1 and isinstance(caught[0], StoreUnavailableError)
        assert "SG703" in str(caught[0])
        assert s.table(tref("orders")) is None  # the pool works again once released


def test_unreachable_store_is_unavailable() -> None:
    from schemaguru.errors import StoreUnavailableError
    from schemaguru.store.postgres import PostgresStore

    require_driver("postgres", "psycopg")
    with pytest.raises(StoreUnavailableError, match="SG703") as info:
        PostgresStore("postgresql://sg:s3cret-pw@127.0.0.1:1/none", connect_timeout_s=2)
    assert "s3cret" not in str(info.value)


def test_bulk_upsert_pages_are_indexed(store: Any) -> None:
    store.upsert_tables([tbl(f"t{i:04d}", f"filler {i}") for i in range(2300)])
    store.upsert_tables([tbl("t2299", "the refunds ledger")])
    assert [r.table for r, _ in store.search("refunds", 3)] == ["t2299"]
    assert [r.table for r, _ in store.search("t1500", 3)][:1] == ["t1500"]
    assert sum(1 for _ in store.iter_tables()) == 2300


def test_pg_v1_store_migrates(pg_schema: tuple[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    """A store at pg version 1 (0.1.0's schema) migrates on open: facts unchanged, and lineage
    facts written before the index existed get their edges."""
    from schemaguru._util.hashing import canonical_json
    from schemaguru.model.facts import Fact, Provenance, Status
    from schemaguru.model.kinds import FactKind
    from schemaguru.model.lineage import LineageEdge, table_lineage_value
    from schemaguru.model.refs import ObjectRef
    from schemaguru.store.postgres import PostgresStore

    dsn, schema = pg_schema
    v1 = schema_sql.migrations("pg")[:1]
    desc = Fact.make(
        FactKind.DESCRIPTION, tref("orders"), {"text": "one row per order"}, Provenance.HUMAN,
        Status.UNVERIFIED,
    )  # fmt: skip
    lin = Fact.make(
        FactKind.LINEAGE, ObjectRef(None, "shop", "v"), table_lineage_value(["shop.orders"]),
        Provenance.DECLARED, Status.UNVERIFIED, source="dbt:manifest:model.shop.v",
    )  # fmt: skip
    with monkeypatch.context() as m:
        m.setattr(schema_sql, "migrations", lambda dialect="sqlite": v1)
        m.setattr(schema_sql, "LATEST_PG_VERSION", 1)
        with PostgresStore(dsn, schema=schema) as old:
            assert old.schema_version() == 1
            old.upsert_tables([tbl("orders")])
            old.put_facts([desc])
            old._x(
                "INSERT INTO facts(id, kind, subject_key, data) VALUES (%s,%s,%s,%s)",
                (lin.id, "LINEAGE", "shop.v", canonical_json(lin.to_dict())),
            )
            before = [f.to_dict() for f in old.iter_facts()]
    with PostgresStore(dsn, schema=schema) as s:
        assert s.schema_version() == 2
        assert [f.to_dict() for f in s.iter_facts()] == before
        assert s.lineage_edges(["shop.orders"], direction="downstream", level="table") == [
            LineageEdge("shop.orders", "shop.v", "table", lin.id)
        ]
        assert [r for r, _ in s.search("order", 5)] == [tref("orders")]
