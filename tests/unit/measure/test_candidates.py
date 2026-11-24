from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import duckdb
import pytest

from schemaguru.catalog import DBAPIExecutor, crawl
from schemaguru.catalog.readers.duckdb import DuckDBReader
from schemaguru.measure.candidates import join_candidates
from schemaguru.model.facts import Fact, Provenance, Status
from schemaguru.model.joins import JoinRef
from schemaguru.model.kinds import FactKind
from schemaguru.model.refs import ObjectRef
from schemaguru.model.structure import Column, Schema, Table
from schemaguru.store.sqlite import SQLiteStore
from tests.fixtures.shop import ShopFacts


def col(t: str, c: str, schema: str = "shop") -> ObjectRef:
    return ObjectRef(None, schema, t, c)


def J(lt: str, lc: str, rt: str, rc: str, cast: str | None = None) -> JoinRef:
    return JoinRef((col(lt, lc),), (col(rt, rc),), cast)


@pytest.fixture
def live_store(tmp_path: Path, shop_duckdb: tuple[Any, ShopFacts]) -> Iterator[SQLiteStore]:
    """A live crawl of the shop DuckDB: PKs are declared there, foreign keys are not."""
    conn, _ = shop_duckdb
    with SQLiteStore(tmp_path / "live.db") as store:
        crawl(DuckDBReader(DBAPIExecutor(conn)), store)
        yield store


def test_live_crawl_has_no_fk_constraints(live_store: SQLiteStore) -> None:
    cons = live_store.constraints(ObjectRef(None, "shop", "orders"))
    assert cons and all(c.kind != "foreign_key" for c in cons)


def test_candidates_heuristic_and_cast(live_store: SQLiteStore, tmp_path: Path) -> None:
    got = join_candidates(live_store, ObjectRef(None, "shop", "orders"))
    assert (J("orders", "customer_id", "customers", "customer_id"), "heuristic") in got
    assert (J("orders", "account_id", "accounts", "account_id"), "heuristic") in got
    assert all(label == "heuristic" for _, label in got)
    assert all(j.left[0].table == "orders" for j, _ in got)
    assert not any(j.right[0].table == "orders" for j, _ in got)  # never a self-join
    ev = join_candidates(live_store, ObjectRef(None, "shop", "events"))
    assert (J("events", "session_id", "sessions", "session_id"), "heuristic") in ev

    # cross-family keys (INTEGER -> VARCHAR) are candidates measured with CAST string
    conn = duckdb.connect()
    conn.execute("CREATE SCHEMA app")
    conn.execute("CREATE TABLE app.users(user_id VARCHAR PRIMARY KEY, active BOOLEAN)")
    conn.execute("CREATE TABLE app.devices(id INTEGER PRIMARY KEY)")
    conn.execute(
        "CREATE TABLE app.logins(login_id INTEGER PRIMARY KEY, user_id INTEGER, "
        '"deviceId" INTEGER, active_fk DATE)'
    )
    with SQLiteStore(tmp_path / "x.db") as st:
        crawl(DuckDBReader(DBAPIExecutor(conn)), st)
        c = join_candidates(st, ObjectRef(None, "app", "logins"))
    conn.close()
    a = ObjectRef(None, "app", "logins")
    assert (
        JoinRef(
            (a.__class__(None, "app", "logins", "user_id"),),
            (ObjectRef(None, "app", "users", "user_id"),),
            "string",
        ),
        "heuristic",
    ) in c
    # camelCase stem -> plural table with an ``id`` PK, same family: no cast
    assert (
        JoinRef(
            (ObjectRef(None, "app", "logins", "deviceId"),),
            (ObjectRef(None, "app", "devices", "id"),),
        ),
        "heuristic",
    ) in c
    # incompatible families (DATE vs anything) are not candidates
    assert not any(j.left[0].column == "active_fk" for j, _ in c)


def test_candidates_labels_and_priority(shop_store: SQLiteStore) -> None:
    orders = ObjectRef(None, "shop", "orders")
    human = J("orders", "status", "products", "name")
    dbt = J("orders", "created_at", "sessions", "started_at")
    llm = J("orders", "order_id", "payments", "order_id")
    for j, prov, src in (
        (human, Provenance.HUMAN, "overlay:schemaguru.yaml:3"),
        (dbt, Provenance.DECLARED, "dbt:manifest:test.x"),
        (llm, Provenance.DRAFTED, "llm:model"),
    ):
        shop_store.put_facts(
            [
                Fact.make(
                    FactKind.JOIN, j, {"cardinality": None}, prov, Status.UNVERIFIED, source=src
                )
            ]
        )
    got = join_candidates(shop_store, orders)
    labels = dict(got)
    # declared FKs come first and win over the same join found by a heuristic
    assert labels[J("orders", "customer_id", "customers", "customer_id")] == "constraint"
    assert labels[human] == "overlay" and labels[dbt] == "dbt" and labels[llm] == "llm"
    order = [label for _, label in got]
    rank = {"constraint": 0, "overlay": 1, "dbt": 2, "heuristic": 3, "llm": 4}
    assert order == sorted(order, key=rank.__getitem__)
    assert len({j for j, _ in got}) == len(got)  # no duplicates


def test_candidates_bounded_and_same_catalog(tmp_path: Path) -> None:
    with SQLiteStore(tmp_path / "b.db") as st:
        child = ObjectRef("c1", "s", "child")
        st.upsert_schemas(
            [Schema("c1", "s"), Schema("c2", "s")] + [Schema("c1", f"s{i}") for i in range(8)]
        )
        st.upsert_tables([Table(child, "table")])
        st.upsert_columns(
            child, [Column(ObjectRef("c1", "s", "child", "item_id"), 1, "INTEGER", "integer")]
        )
        # eight schemas in the same catalog each hold an ``items`` table with an ``id``; one in
        # another catalog
        for i in range(8):
            t = ObjectRef("c1", f"s{i}", "items")
            st.upsert_tables([Table(t, "table")])
            st.upsert_columns(
                t, [Column(ObjectRef("c1", f"s{i}", "items", "id"), 1, "INTEGER", "integer")]
            )
        other = ObjectRef("c2", "s", "items")
        st.upsert_tables([Table(other, "table")])
        st.upsert_columns(
            other, [Column(ObjectRef("c2", "s", "items", "id"), 1, "INTEGER", "integer")]
        )
        got = join_candidates(st, child)
        assert len(got) == 5  # <= max_per_column
        assert all(j.right[0].catalog == "c1" for j, _ in got)
        assert len(join_candidates(st, child, max_per_column=2)) == 2
        everywhere = join_candidates(st, child, max_per_column=20, same_catalog_only=False)
        assert any(j.right[0].catalog == "c2" for j, _ in everywhere)


def test_candidates_never_write(shop_store: SQLiteStore) -> None:
    before = len(list(shop_store.iter_facts()))
    join_candidates(shop_store, ObjectRef(None, "shop", "orders"))
    assert len(list(shop_store.iter_facts())) == before
    assert join_candidates(shop_store, ObjectRef(None, "shop", "nope")) == []


def test_float_keys_are_numeric(tmp_path: Path) -> None:
    conn = duckdb.connect()
    conn.execute("CREATE SCHEMA m")
    conn.execute("CREATE TABLE m.products(product_id DOUBLE PRIMARY KEY)")
    conn.execute("CREATE TABLE m.lines(line_id INTEGER PRIMARY KEY, product_id INTEGER)")
    with SQLiteStore(tmp_path / "f.db") as st:
        crawl(DuckDBReader(DBAPIExecutor(conn)), st)
        got = join_candidates(st, ObjectRef(None, "m", "lines"))
    conn.close()
    j = JoinRef((ObjectRef(None, "m", "lines", "product_id"),),
                (ObjectRef(None, "m", "products", "product_id"),))  # fmt: skip
    assert (j, "heuristic") in got  # integer vs float: no cast needed
