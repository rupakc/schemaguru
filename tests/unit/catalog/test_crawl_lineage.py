from __future__ import annotations

import hashlib
import importlib
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from schemaguru.catalog.crawl import LAST_KEY, crawl
from schemaguru.config import CrawlConfig
from schemaguru.lineage import ParserPool
from schemaguru.model.facts import Fact, Provenance, Status
from schemaguru.model.kinds import LINEAGE_KINDS
from schemaguru.model.lineage import VIEW_SOURCE
from schemaguru.model.refs import ObjectRef
from schemaguru.store.sqlite import SQLiteStore
from tests.unit.catalog.fake_reader import ViewFakeReader

crawl_module = importlib.import_module("schemaguru.catalog.crawl")
NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)
REPO = Path(__file__).resolve().parents[3]
HOSTILE = [sys.executable, "-m", "tests.unit.lineage.hostile_worker"]

V = ObjectRef(None, "shop", "v_orders")
V_SQL = "CREATE VIEW shop.v_orders AS SELECT id, amount FROM shop.orders"


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SQLiteStore]:
    s = SQLiteStore(tmp_path / "t.db")
    yield s
    s.close()


def shop(**kw: Any) -> ViewFakeReader:
    return ViewFakeReader(
        {
            "shop.orders": ["id", "amount"],
            "shop.customers": ["id", "email"],
            "shop.v_orders": ["id", "amount"],
        },
        {"shop.v_orders": V_SQL},
        **kw,
    )


def view_facts(store: SQLiteStore, view: ObjectRef = V) -> list[Fact]:
    subjects = [view, *(c.ref for c in store.columns(view))]
    return [f for f in store.facts_for(subjects, LINEAGE_KINDS) if f.source == VIEW_SOURCE]


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def test_views_get_lineage_facts(store: SQLiteStore) -> None:
    report = crawl(shop(), store, now=lambda: NOW)
    assert report.errors == [] and report.lineage_diagnostics == []
    assert report.views_parsed == 1 and report.lineage_s > 0
    facts = {f.subject.canonical(): f for f in view_facts(store)}  # type: ignore[union-attr]
    table = facts["shop.v_orders"]
    assert table.kind.name == "LINEAGE" and table.value["upstream"] == ["shop.orders"]
    assert table.provenance is Provenance.CATALOG and table.status is Status.UNVERIFIED
    assert table.observed_at == NOW
    assert table.evidence is not None and table.evidence.sql == V_SQL
    col = facts["shop.v_orders.amount"]
    assert col.value["inputs"] == ["shop.orders.amount"] and col.value["transform"] == "copy"
    assert set(facts) == {"shop.v_orders", "shop.v_orders.id", "shop.v_orders.amount"}
    assert store.lineage_state_get([V]) == {V: sha(V_SQL)}


def test_unchanged_definition_not_reparsed(
    store: SQLiteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = shop()
    crawl(reader, store, now=lambda: NOW)
    before = view_facts(store)
    calls: list[int] = []
    real = ParserPool.map

    def spy(self: ParserPool, requests: Any) -> Any:
        calls.append(len(requests))
        return real(self, requests)

    monkeypatch.setattr(ParserPool, "map", spy)
    report = crawl(reader, store, now=lambda: NOW + timedelta(days=1))
    assert calls == [] and report.views_parsed == 0
    assert len(reader.definition_calls) == 2  # a full crawl re-reads every definition
    assert view_facts(store) == before


def test_changed_definition_replaces_facts(
    store: SQLiteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = shop()
    crawl(reader, store, now=lambda: NOW)
    old = {f.id for f in view_facts(store)}
    reader.views["shop.v_orders"] = (
        "CREATE VIEW shop.v_orders AS SELECT id, email AS amount FROM shop.customers"
    )

    txn: list[int] = [0, 0]  # depth, current outer transaction number
    seen: list[tuple[str, int]] = []
    real_txn, real_put, real_del = store.transaction, store.put_facts, store.delete_facts

    @contextmanager
    def transaction() -> Iterator[None]:
        if txn[0] == 0:
            txn[1] += 1
        txn[0] += 1
        try:
            with real_txn():
                yield
        finally:
            txn[0] -= 1

    def put_facts(facts: Any) -> None:
        facts = list(facts)
        if any(f.kind.name in LINEAGE_KINDS for f in facts):
            seen.append(("put", txn[1] if txn[0] else 0))
        real_put(facts)

    def delete_facts(ids: Any) -> int:
        ids = list(ids)
        if set(ids) & old:
            seen.append(("delete", txn[1] if txn[0] else 0))
        return real_del(ids)

    monkeypatch.setattr(store, "transaction", transaction)
    monkeypatch.setattr(store, "put_facts", put_facts)
    monkeypatch.setattr(store, "delete_facts", delete_facts)
    report = crawl(reader, store, now=lambda: NOW + timedelta(days=1))
    assert report.views_parsed == 1
    new = view_facts(store)
    assert {f.id for f in new}.isdisjoint(old)
    assert {f.subject.canonical(): f.value.get("upstream") for f in new}[  # type: ignore[union-attr]
        "shop.v_orders"
    ] == ["shop.customers"]
    assert [k for k, _ in seen] == ["delete", "put"]
    assert seen[0][1] == seen[1][1] != 0  # one transaction


def test_dropped_view_retracts_lineage(store: SQLiteStore) -> None:
    reader = shop()
    crawl(reader, store, now=lambda: NOW)
    assert view_facts(store)
    del reader.relations["shop.v_orders"], reader.views["shop.v_orders"]
    report = crawl(reader, store, now=lambda: NOW + timedelta(days=1))
    assert report.withdrawn == 1
    assert view_facts(store) == []
    assert store.lineage_state_get([V]) == {}
    assert store.lineage_edges(["shop.orders"], direction="downstream", level="table") == []


def test_failure_is_sg211_and_crawl_continues(
    store: SQLiteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(REPO)
    reader = shop()
    reader.relations["shop.v_bad"] = ["id"]
    reader.views["shop.v_bad"] = "CREATE VIEW shop.v_bad AS SELECT id FROM shop.orders"
    bad = ObjectRef(None, "shop", "v_bad")
    with ParserPool(timeout_s=2, command=HOSTILE) as pool:
        crawl(reader, store, now=lambda: NOW, parser_pool=pool)
        assert view_facts(store, bad)
        reader.views["shop.v_bad"] = "DIE"
        reader.views["shop.v_orders"] = V_SQL + " WHERE amount > 0"
        report = crawl(reader, store, now=lambda: NOW + timedelta(days=1), parser_pool=pool)
        assert report.errors == []
        assert [(d.code, d.location) for d in report.lineage_diagnostics] == [
            ("SG211", "shop.v_bad")
        ]
        assert "DIE" not in report.lineage_diagnostics[0].message
        assert report.views_parsed == 1  # v_orders still parsed after the worker died
        assert view_facts(store, bad) == []  # stale facts of the old definition are gone
        assert store.lineage_state_get([bad]) == {}  # retried next crawl
        assert view_facts(store)
        again = crawl(reader, store, now=lambda: NOW + timedelta(days=2), parser_pool=pool)
        assert [d.code for d in again.lineage_diagnostics] == ["SG211"]


def test_resume_parses_views_from_earlier_run(store: SQLiteStore) -> None:
    reader = shop(interrupt_schema="tail")
    reader.relations["tail.t"] = ["id"]
    with pytest.raises(KeyboardInterrupt):
        crawl(reader, store, now=lambda: NOW)
    assert store.table(V) is not None and view_facts(store) == []
    report = crawl(reader, store, now=lambda: NOW)
    assert report.tables_written == 1  # only the schema after the cursor
    assert report.views_parsed == 1 and view_facts(store)


def test_incremental_reads_only_changed_views(store: SQLiteStore) -> None:
    reader = shop(incremental=True)
    reader.relations["shop.v_two"] = ["id"]
    reader.views["shop.v_two"] = "CREATE VIEW shop.v_two AS SELECT id FROM shop.customers"
    crawl(reader, store, now=lambda: NOW)
    assert sorted(reader.definition_calls[0]) == ["shop.v_orders", "shop.v_two"]
    reader.views["shop.v_orders"] = (
        "CREATE VIEW shop.v_orders AS SELECT id, id AS amount FROM shop.customers"
    )
    reader.altered["shop.v_orders"] = NOW + timedelta(hours=1)
    report = crawl(reader, store, now=lambda: NOW + timedelta(days=1))
    assert reader.definition_calls[1:] == [["shop.v_orders"]]
    assert report.views_parsed == 1


def test_without_extra_one_sg212(store: SQLiteStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(crawl_module, "lineage_available", lambda: False)
    reader = shop()
    reader.relations["shop.v_two"] = ["id"]
    reader.views["shop.v_two"] = "CREATE VIEW shop.v_two AS SELECT id FROM shop.customers"
    report = crawl(reader, store, now=lambda: NOW)
    assert reader.definition_calls == []
    assert [d.code for d in report.lineage_diagnostics] == ["SG212"]
    assert "`lineage` extra" in report.lineage_diagnostics[0].message
    assert view_facts(store) == [] and store.lineage_state_get([V]) == {}
    assert report.views_parsed == 0


def test_no_sg212_without_the_capability(
    store: SQLiteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(crawl_module, "lineage_available", lambda: False)
    report = crawl(shop(definitions=False), store, now=lambda: NOW)
    assert report.lineage_diagnostics == []


def test_lineage_false_reads_no_definitions(store: SQLiteStore) -> None:
    reader = shop()
    report = crawl(reader, store, now=lambda: NOW, lineage=False)
    assert reader.definition_calls == [] and view_facts(store) == []
    assert report.views_parsed == 0


def test_lineage_diagnostics_do_not_block_baseline(
    store: SQLiteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(crawl_module, "lineage_available", lambda: False)
    report = crawl(shop(), store, now=lambda: NOW)
    assert report.errors == [] and report.lineage_diagnostics
    assert store.crawl_state_get(LAST_KEY) is not None


def test_definition_read_error_is_sg102_without_its_message(store: SQLiteStore) -> None:
    report = crawl(shop(fail_definitions=True), store, now=lambda: NOW)
    assert [d.code for d in report.errors] == ["SG102"]
    text = report.errors[0].message
    assert "secret_column" not in text and "1234" not in text and "driver said" not in text
    assert store.crawl_state_get(LAST_KEY) is None


def test_out_of_scope_upstream_is_unresolved(store: SQLiteStore) -> None:
    reader = shop()
    reader.relations["staging_raw.orders"] = ["id"]
    reader.views["shop.v_orders"] = "CREATE VIEW shop.v_orders AS SELECT id FROM staging_raw.orders"
    cfg = CrawlConfig(exclude=(*CrawlConfig().exclude, "staging_raw.*"))
    crawl(reader, store, config=cfg, now=lambda: NOW)
    assert store.table(ObjectRef(None, "staging_raw", "orders")) is None
    table = next(f for f in view_facts(store) if f.kind.name == "LINEAGE")
    assert table.value["upstream"] == []
    assert tuple(table.value["unresolved"]) == ("staging_raw.orders",)


@pytest.mark.parametrize("incremental", [False, True])
def test_resume_after_change_reparses(store: SQLiteStore, incremental: bool) -> None:
    reader = shop(incremental=incremental)
    reader.relations["tail.t"] = ["id"]
    crawl(reader, store, now=lambda: NOW)
    reader.views["shop.v_orders"] = (
        "CREATE VIEW shop.v_orders AS SELECT id, email AS amount FROM shop.customers"
    )
    reader.altered["shop.v_orders"] = NOW + timedelta(hours=1)
    reader.interrupt_schema = "tail"
    with pytest.raises(KeyboardInterrupt):
        crawl(reader, store, now=lambda: NOW + timedelta(days=1))
    report = crawl(reader, store, now=lambda: NOW + timedelta(days=1))
    assert report.views_parsed == 1
    table = next(f for f in view_facts(store) if f.kind.name == "LINEAGE")
    assert table.value["upstream"] == ["shop.customers"]
    # a resumed crawl offers every view, but unchanged definitions are still not re-parsed
    reader.interrupt_schema = "tail"
    with pytest.raises(KeyboardInterrupt):
        crawl(reader, store, now=lambda: NOW + timedelta(days=2))
    assert crawl(reader, store, now=lambda: NOW + timedelta(days=2)).views_parsed == 0


def test_dropped_view_column_retracts_its_lineage(store: SQLiteStore) -> None:
    reader = shop()
    crawl(reader, store, now=lambda: NOW)
    amount = ObjectRef(None, "shop", "v_orders", "amount")
    assert store.facts_for([amount], LINEAGE_KINDS)
    reader.relations["shop.v_orders"] = ["id"]
    reader.views["shop.v_orders"] = "CREATE VIEW shop.v_orders AS SELECT id FROM shop.orders"
    crawl(reader, store, now=lambda: NOW + timedelta(days=1))
    assert store.facts_for([amount], LINEAGE_KINDS) == []
    assert store.lineage_edges(["shop.orders.amount"], direction="downstream", level="column") == []
    assert store.lineage_edges(["shop.orders.id"], direction="downstream", level="column")


def test_dropped_view_retracts_lineage_of_dropped_columns(store: SQLiteStore) -> None:
    reader = shop()
    crawl(reader, store, now=lambda: NOW)
    reader.relations["shop.v_orders"] = ["id"]
    reader.views["shop.v_orders"] = "CREATE VIEW shop.v_orders AS SELECT id FROM shop.orders"
    crawl(reader, store, now=lambda: NOW + timedelta(days=1), lineage=False)  # columns only
    assert store.facts_for([ObjectRef(None, "shop", "v_orders", "amount")], LINEAGE_KINDS)
    del reader.relations["shop.v_orders"], reader.views["shop.v_orders"]
    crawl(reader, store, now=lambda: NOW + timedelta(days=2))
    assert store.lineage_facts_under([V]) == []


def test_missing_definition_retracts_and_reports(store: SQLiteStore) -> None:
    reader = shop()
    crawl(reader, store, now=lambda: NOW)
    assert view_facts(store)
    reader.withheld.add("shop.v_orders")
    report = crawl(reader, store, now=lambda: NOW + timedelta(days=1))
    assert report.errors == []
    assert [(d.code, d.location) for d in report.lineage_diagnostics] == [
        ("SG211", "shop.v_orders")
    ]
    assert report.lineage_diagnostics[0].message.endswith(": definition_unavailable")
    assert store.lineage_facts_under([V]) == []
    assert store.lineage_state_get([V]) == {}
