"""The real FactLang behind ``FactLangBackend`` on the shop DuckDB.

Each test skips with "factlang capability <name> not yet available" until the installed FactLang
provides it (global constraints, "FactLang availability"); Task 25 removes the skips.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from schemaguru.config import Config, SamplingConfig
from schemaguru.measure import flbridge
from schemaguru.measure.catalog_bridge import StoreCatalog
from schemaguru.measure.convert import to_facts
from schemaguru.measure.requests import (
    KeyOverlapReq,
    ProfileColumns,
    Request,
    SampleChoice,
    TopValues,
    Uniqueness,
)
from schemaguru.measure.scheduler import Scheduler
from schemaguru.model.facts import Fact, Status
from schemaguru.model.joins import JoinRef
from schemaguru.model.refs import ObjectRef
from schemaguru.model.structure import Column, Schema, Table
from schemaguru.store.sqlite import SQLiteStore
from tests.fixtures.shop import ShopFacts

pytestmark = pytest.mark.factlang
NOW = datetime(2026, 11, 24, tzinfo=timezone.utc)


def need(*names: str) -> None:
    caps = flbridge.capabilities()
    for name in names:
        if name not in caps:
            pytest.skip(f"factlang capability {name} not yet available")


def col(t: str, c: str) -> ObjectRef:
    return ObjectRef(None, "shop", t, c)


def connector(conn: Any) -> Any:
    need("measure")
    import factlang.connectors as flc  # the seam's tests may touch FactLang directly

    return flc.DBAPIConnector(conn, "duckdb")


def measure(backend: flbridge.FactLangBackend, req: Request) -> list[Fact]:
    (res,) = backend.measure([req])
    assert res.error is None, res.error
    return to_facts(res, observed_at=NOW, snapshot_id="s", config=Config())


def test_plans_translate_every_request() -> None:
    """IR construction only (no execution): every request maps to valid FactLang plans."""
    need("composite_join", "sample")
    ir = flbridge._IR()
    key = SampleChoice("keyhash", Decimal("0.1"), 3, ("customer_id",))
    j = JoinRef((col("orders", "customer_id"),), (col("customers", "customer_id"),))
    plans = flbridge.plans_for(KeyOverlapReq(j, sample=key), ir)
    (ko,) = plans
    assert ko.sample.key[0].text == "customer_id" and ko.right_sample.key[0].text == "customer_id"
    assert ko.sample.seed == ko.right_sample.seed == 3
    assert ko.sample.fraction == ko.right_sample.fraction == Decimal("0.1")
    prof = flbridge.plans_for(ProfileColumns(ObjectRef(None, "shop", "orders"), ("a", "b")), ir)
    assert len(prof) == 2 and all(p.sample is None for p in prof)
    (vm,) = flbridge.plans_for(TopValues(col("orders", "status"), 20), ir)
    assert vm.declared == () and vm.report_limit == 20
    (ck,) = flbridge.plans_for(
        Uniqueness(ObjectRef(None, "shop", "order_items"), ("order_id", "line_no")), ir
    )
    assert [c.text for c in ck.columns] == ["order_id", "line_no"]


def test_shop_join_numbers_exact(
    shop_duckdb: tuple[Any, ShopFacts], shop_store: SQLiteStore
) -> None:
    conn, facts = shop_duckdb
    backend = flbridge.FactLangBackend(connector(conn), catalog=StoreCatalog(shop_store))
    oc = facts.joins["orders->customers"]
    (f,) = measure(
        backend,
        KeyOverlapReq(JoinRef((col("orders", "customer_id"),), (col("customers", "customer_id"),))),
    )
    assert f.status is Status.OBSERVED
    assert f.value["row_match_rate"] == pytest.approx(oc.row_match_rate)
    assert f.value["match_rate"] == pytest.approx(oc.key_match_rate)
    rc = facts.joins["ref_child->ref_parent"]
    (g,) = measure(
        backend,
        KeyOverlapReq(JoinRef((col("ref_child", "parent_id"),), (col("ref_parent", "id"),))),
    )
    assert g.value["match_rate"] == pytest.approx(rc.key_match_rate)
    assert g.value["coverage"] == pytest.approx(rc.coverage)


def test_shop_case_drift_values(
    shop_duckdb: tuple[Any, ShopFacts], shop_store: SQLiteStore
) -> None:
    conn, facts = shop_duckdb
    backend = flbridge.FactLangBackend(connector(conn), catalog=StoreCatalog(shop_store))
    (f,) = measure(backend, TopValues(col("orders", "status"), 20))
    got = {v["value"]: v["count"] for v in f.value["observed"]}
    assert got == facts.status_counts and "PAID" in got and "paid" in got


def test_big_t_sampled_estimate_within_interval(shop_duckdb_big: Any, tmp_path: Path) -> None:
    need("measure", "sample")
    conn = shop_duckdb_big
    big = ObjectRef(None, "shop", "big_t")
    with SQLiteStore(tmp_path / "big.db") as store:
        store.upsert_schemas([Schema(None, "shop")])
        store.upsert_tables([Table(big, "table", row_count=2_000_000)])
        store.upsert_columns(
            big,
            [
                Column(ObjectRef(None, "shop", "big_t", c), i, "INTEGER", "integer")
                for i, c in enumerate(("id", "k", "v"), 1)
            ],
        )
        backend = flbridge.FactLangBackend(connector(conn), catalog=StoreCatalog(store))
        cfg = Config(sampling=SamplingConfig(measurement="force"))
        Scheduler(store, backend, cfg).run([big])
        (nr,) = store.facts_for([ObjectRef(None, "shop", "big_t", "v")], ["NULL_RATE"])
        assert nr.status is Status.ESTIMATED
        true_rate = conn.execute("SELECT 1 - COUNT(v) / COUNT(*) FROM shop.big_t").fetchone()[0]
        est = nr.value["estimate"]
        assert est["low"] <= float(true_rate) <= est["high"]
        (dk,) = store.facts_for([ObjectRef(None, "shop", "big_t", "k")], ["DISTINCT"])
        assert dk.value["estimate"]["lower_bound"] is True and dk.value["distinct_keys"] <= 1000


class _Ev:
    def __init__(self, b: int | None) -> None:
        self.bytes_processed = b
        self.sql = "SELECT 1"

    def to_dict(self) -> dict[str, Any]:
        return {"bytes_processed": self.bytes_processed}


class _M:
    def __init__(self, plan: Any, payload: Any, error: Any = None, b: int | None = 10) -> None:
        self.plan, self.payload, self.error, self.evidence = plan, payload, error, _Ev(b)


class _Err:
    code, message = "FL701", "over cost budget"


class _FakeChecker:
    """Stands in for ``factlang.Checker`` so the bridge's batching and assembly are exercised
    before FactLang ships ``Checker.measure`` (plans are real FactLang IR)."""

    def measure(self, plans: list[Any]) -> list[_M]:
        out = []
        for p in plans:
            name = type(p).__name__
            if name == "ValueMembership":
                out.append(
                    _M(
                        p,
                        {
                            "rows": [
                                {
                                    "kind": "summary",
                                    "non_null": Decimal(5412),
                                    "declared_rows": 0,
                                    "undeclared_distinct": 2,
                                    "value": None,
                                    "freq": None,
                                },
                                {"kind": "undeclared", "value": "paid", "freq": Decimal(5000)},
                                {"kind": "undeclared", "value": "PAID", "freq": 412},
                            ]
                        },
                    )
                )
            elif name == "KeyOverlap":
                out.append(_M(p, None, _Err()))
            else:
                out.append(_M(p, {"row_count": Decimal(10), "non_null": 9, "distinct_keys": 3}))
        return out

    def estimate(self, plans: list[Any]) -> list[Any]:
        return [type("E", (), {"bytes": 7})() for _ in plans]


def test_bridge_batches_and_assembles() -> None:
    need("composite_join", "sample")
    b = object.__new__(flbridge.FactLangBackend)
    b._caps = frozenset({"measure", "estimate", "sample", "composite_join"})
    b._checker = _FakeChecker()
    b._ir = flbridge._IR()
    t = ObjectRef(None, "shop", "orders")
    j = JoinRef((col("orders", "customer_id"),), (col("customers", "customer_id"),))
    prof, top, ko = b.measure(
        [ProfileColumns(t, ("a", "b")), TopValues(col("orders", "status"), 20), KeyOverlapReq(j)]
    )
    assert prof.payload == {
        "row_count": 10,
        "columns": {
            "a": {"non_null": 9, "distinct_keys": 3},
            "b": {"non_null": 9, "distinct_keys": 3},
        },
    }
    assert prof.evidence is not None and prof.evidence.bytes_processed == 20
    assert top.payload == {
        "non_null": 5412,
        "distinct": 2,
        "values": [{"value": "paid", "count": 5000}, {"value": "PAID", "count": 412}],
    }
    assert ko.payload is None and ko.error is not None and ko.error.code == "SG301"
    (est,) = b.estimate([ProfileColumns(t, ("a", "b"))])
    assert est.bytes == 14 and est.method == "dry_run"
