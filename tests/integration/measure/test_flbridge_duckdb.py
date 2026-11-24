"""The real FactLang behind ``FactLangBackend`` on the shop DuckDB.

FactLang ``main`` ships the IR, the dialect renderers, ``DBAPIConnector`` and
``judge.run.payload_for`` but not yet ``Checker.measure``/``estimate`` (spec 18 R1/R5). Until it
does, the ``fl_checker`` fixture installs a stand-in ``factlang.Checker`` built ONLY from those real
FactLang parts (render -> connector -> payload_for), so every plan SchemaGuru builds is rendered by
FactLang's own DuckDB profile and every payload has FactLang's real wire-format aliases. Once the
real ``Checker.measure`` is installed the fixture steps aside and these tests run against it.
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


class _Measurement:
    def __init__(self, plan: Any, payload: Any, evidence: Any, error: Any) -> None:
        self.plan, self.payload, self.evidence, self.error = plan, payload, evidence, error


class _FLError:
    def __init__(self, exc: Exception) -> None:
        text = str(exc)
        self.code = text.split(" ", 1)[0] if text.startswith("FL") else "FL700"
        self.message = text


class _StandInChecker:
    """``Checker.measure``/``estimate`` per spec 9.3, from real FactLang parts only."""

    def __init__(self, connector: Any, *, catalog: Any = None, **_: Any) -> None:
        self.connector = connector

    def measure(self, plans: list[Any]) -> list[_Measurement]:
        import factlang.sql as fls
        from factlang.judge.outcome import Evidence
        from factlang.judge.run import payload_for

        profile = fls.get_profile(self.connector.dialect)
        out = []
        for p in plans:
            try:
                probe = fls.render(p, profile)
                res = self.connector.execute(probe, timeout_s=None)
                ev = Evidence(
                    sql=probe.sql_template,
                    params=tuple(probe.params),
                    dialect=probe.dialect,
                    tier=probe.tier,
                    bytes_processed=res.bytes_processed,
                )
                out.append(_Measurement(p, payload_for(p.arity, res.rows), ev, None))
            except Exception as e:  # Checker.measure never raises for a failing plan
                out.append(_Measurement(p, None, None, _FLError(e)))
        return out

    def estimate(self, plans: list[Any]) -> list[Any]:  # no dry-run on DuckDB: never a guess
        return [type("CostEstimate", (), {"bytes": None, "method": "unknown"})() for _ in plans]


@pytest.fixture
def fl_checker(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real ``factlang.Checker`` when it can measure, else the stand-in above."""
    import factlang

    if "measure" not in flbridge.capabilities():
        monkeypatch.setattr(factlang, "Checker", _StandInChecker, raising=False)
    assert "measure" in flbridge.capabilities()


def need(*names: str) -> None:
    caps = flbridge.capabilities()
    for name in names:
        if name not in caps:
            pytest.skip(f"factlang capability {name} not yet available")


def col(t: str, c: str) -> ObjectRef:
    return ObjectRef(None, "shop", t, c)


def connector(conn: Any) -> Any:
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
    # a 64-bit seed is folded into the dialect's seed range, alike on every call (FL506)
    big = SampleChoice("bernoulli", Decimal("0.1"), 2**64 - 1, ())
    folded = {flbridge._IR((0, 100)).sample(big).seed for _ in range(3)}
    assert len(folded) == 1 and 0 <= folded.pop() <= 100


def test_shop_join_numbers_exact(
    shop_duckdb: tuple[Any, ShopFacts], shop_store: SQLiteStore, fl_checker: None
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
    shop_duckdb: tuple[Any, ShopFacts], shop_store: SQLiteStore, fl_checker: None
) -> None:
    conn, facts = shop_duckdb
    backend = flbridge.FactLangBackend(connector(conn), catalog=StoreCatalog(shop_store))
    (f,) = measure(backend, TopValues(col("orders", "status"), 20))
    got = {v["value"]: v["count"] for v in f.value["observed"]}
    assert got == facts.status_counts and "PAID" in got and "paid" in got


def test_big_t_sampled_estimate_within_interval(
    shop_duckdb_big: Any, tmp_path: Path, fl_checker: None
) -> None:
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
    b._sampling = SamplingConfig()
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


def test_bridge_short_measurement_list_is_an_error() -> None:
    need("composite_join", "sample")

    class Short(_FakeChecker):
        def measure(self, plans: list[Any]) -> list[_M]:
            return super().measure(plans)[:-1]

    b = object.__new__(flbridge.FactLangBackend)
    b._sampling = SamplingConfig()
    b._caps = frozenset({"measure", "sample", "composite_join"})
    b._checker = Short()
    b._ir = flbridge._IR()
    t = ObjectRef(None, "shop", "orders")
    res = b.measure([ProfileColumns(t, ("a", "b")), TopValues(col("orders", "status"), 20)])
    assert all(r.payload is None and r.error is not None and r.error.code == "SG303" for r in res)


def test_duckdb_backend_declares_no_block_sampling(shop_duckdb: tuple[Any, ShopFacts]) -> None:
    """FactLang's DuckDB profile refuses ``system`` samples (FL506): the backend says so, so the
    scheduler picks ``bernoulli`` instead of a sample FactLang cannot render."""
    from schemaguru.measure.scheduler import NO_BLOCK_SAMPLING

    conn, _ = shop_duckdb
    b = flbridge.FactLangBackend(connector(conn), catalog=None)
    assert NO_BLOCK_SAMPLING in b.capabilities()
    assert NO_BLOCK_SAMPLING not in flbridge.capabilities()  # a per-connector fact only


def test_row_count_only_profile(
    shop_duckdb: tuple[Any, ShopFacts], shop_store: SQLiteStore, fl_checker: None
) -> None:
    conn, _ = shop_duckdb
    backend = flbridge.FactLangBackend(connector(conn), catalog=StoreCatalog(shop_store))
    (res,) = backend.measure([ProfileColumns(ObjectRef(None, "shop", "orders"), ())])
    assert res.error is None, res.error
    assert res.payload == {
        "row_count": conn.execute("SELECT COUNT(*) FROM shop.orders").fetchone()[0],
        "columns": {},
    }


def test_real_evidence_params_are_scrubbed_and_json_safe(
    shop_duckdb: tuple[Any, ShopFacts], shop_store: SQLiteStore, fl_checker: None
) -> None:
    """FactLang evidence carries ``Param`` objects; stored evidence holds scrubbed plain dicts."""
    import json

    from schemaguru.measure.requests import PatternRate

    conn, _ = shop_duckdb
    backend = flbridge.FactLangBackend(connector(conn), catalog=StoreCatalog(shop_store))
    secret = "%jane.doe@example.com%"
    (res,) = backend.measure([PatternRate(col("customers", "email"), secret)])
    assert res.error is None, res.error
    ev = res.evidence
    assert ev is not None and ev.params, ev
    text = json.dumps(ev.to_dict())
    assert "jane.doe@example.com" not in text
    # FactLang's Measurement evidence carries plain values; its Outcome/probe evidence carries
    # ``Param`` objects (name, family, value): either way stored as plain, scrubbed JSON
    assert all(
        not hasattr(p, "family")
        and (not isinstance(p, dict) or {"name", "family", "value"} <= set(p))
        for p in ev.params
    )
    param_obj = type(
        "Param", (), {"name": "p1", "family": "string", "value": "jane.doe@example.com"}
    )
    (scrubbed,) = flbridge._scrub_params((param_obj(),), "masked")
    assert scrubbed["name"] == "p1" and "jane.doe" not in json.dumps(scrubbed)


def test_guru_end_to_end_through_factlang(
    tmp_path: Path, shop_duckdb: tuple[Any, ShopFacts], fl_checker: None
) -> None:
    """crawl (FactLang ``run_metadata``) -> measure (``FactLangBackend``) -> context."""
    import schemaguru as sg
    from schemaguru.config import Budget

    conn, facts = shop_duckdb
    # DuckDB has no dry-run and its catalog reports no bytes, so every probe is of unknown cost
    # and the default budget (charged pessimistically, 1 GiB each of 10 GiB) admits only 10
    cfg = Config(budget=Budget(max_bytes_total=None))
    guru = sg.Guru(connector=connector(conn), store=str(tmp_path / "shop.db"), config=cfg)
    try:
        crawled = guru.crawl()
        assert crawled.tables_seen >= 9 and not crawled.errors, crawled.errors
        measured = guru.measure(all=True)
        assert not measured.errors, measured.errors
        guru.load_overlays(
            Path(__file__).parents[2] / "fixtures" / "overlays" / "spec_example.yaml"
        )
        text = guru.context("refund rate by month").text
        start = text.index("[shop.orders]")
        end = text.find("\n\n[", start + 1)
        orders = text[start : end if end != -1 else len(text)]
        assert "case drift: 'paid'/'PAID' differ only in case" in orders
        assert f"observed also 'PAID' ({facts.status_counts['PAID']:,} rows)" in orders
        assert "← shop.payments.order_id (coverage 5%)" in orders
        # FactLang reports observed values as text; a boolean column's are booleans again
        deleted = next(ln for ln in orders.splitlines() if ln.strip().startswith("deleted "))
        assert f"values: TRUE ({facts.deleted_true})" in deleted and "'true'" not in deleted
        assert "unmeasured" not in orders
    finally:
        guru.close()
