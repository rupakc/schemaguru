from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from schemaguru.catalog import DBAPIExecutor, crawl
from schemaguru.catalog.readers.duckdb import DuckDBReader
from schemaguru.config import Budget, Config, SamplingConfig
from schemaguru.measure.requests import (
    KeyOverlapReq,
    PatternRate,
    ProfileColumns,
    Request,
    RowCount,
    TopValues,
    Uniqueness,
)
from schemaguru.measure.scheduler import BackgroundDrainer, Scheduler
from schemaguru.model.facts import Fact, Provenance, Status
from schemaguru.model.joins import JoinRef
from schemaguru.model.refs import ObjectRef
from schemaguru.store.sqlite import SQLiteStore
from tests.fixtures.shop import ShopFacts
from tests.unit.measure.fake_backend import FakeBackend

ORD = ObjectRef(None, "shop", "orders")
CUS = ObjectRef(None, "shop", "customers")
PRD = ObjectRef(None, "shop", "products")
O_C = JoinRef(
    (ObjectRef(None, "shop", "orders", "customer_id"),),
    (ObjectRef(None, "shop", "customers", "customer_id"),),
)
O_A = JoinRef(
    (ObjectRef(None, "shop", "orders", "account_id"),),
    (ObjectRef(None, "shop", "accounts", "account_id"),),
)
LARGE_ORDERS = SamplingConfig(sample_threshold_rows=5000)  # orders (10k rows) is "large"


@pytest.fixture
def conn(shop_duckdb: tuple[Any, ShopFacts]) -> Any:
    return shop_duckdb[0]


def facts(store: SQLiteStore, kind: str, subject: ObjectRef | JoinRef) -> list[Fact]:
    return store.facts_for([subject], [kind])


def one(store: SQLiteStore, kind: str, subject: ObjectRef | JoinRef) -> Fact:
    got = facts(store, kind, subject)
    assert len(got) == 1, got
    return got[0]


def col(t: str, c: str) -> ObjectRef:
    return ObjectRef(None, "shop", t, c)


def all_requests(fake: FakeBackend) -> list[Request]:
    return [r for batch in fake.measured for r in batch]


def test_plan_table(shop_store: SQLiteStore, conn: Any) -> None:
    s = Scheduler(shop_store, FakeBackend(conn), Config())
    plan = s.plan_table(ORD)
    kinds = {type(r) for r in plan}
    assert ProfileColumns in kinds and Uniqueness in kinds and KeyOverlapReq in kinds
    assert TopValues not in kinds  # enum discovery is phase 2, after distinct counts are known
    assert RowCount not in kinds  # a small table's row count comes with its profile
    assert Uniqueness(ORD, ("order_id",)) in plan
    assert KeyOverlapReq(O_C) in plan and KeyOverlapReq(O_A) in plan
    assert PatternRate(col("orders", "created_at"), "____-__-__%", "ISO-8601 date text") in plan
    big = Scheduler(shop_store, FakeBackend(conn), Config(sampling=LARGE_ORDERS))
    assert RowCount(ORD) in big.plan_table(ORD)
    assert s.plan_table(ObjectRef(None, "shop", "nope")) == []


def test_exact_small_table_observed(
    shop_store: SQLiteStore, conn: Any, shop_facts: ShopFacts
) -> None:
    fake = FakeBackend(conn)
    report = Scheduler(shop_store, fake, Config()).run([CUS, ORD])
    assert report.measured_tables == [CUS, ORD] and report.sampled_tables == []
    assert report.errors == [] and report.over_budget == [] and report.facts_written > 0
    phone = one(shop_store, "NULL_RATE", col("customers", "phone"))
    assert phone.status is Status.OBSERVED and phone.method == "exact"
    assert phone.value["rate"] == pytest.approx(shop_facts.null_rates["customers.phone"])
    assert one(shop_store, "ROW_COUNT", CUS).value["rows"] == shop_facts.row_counts["customers"]
    j = one(shop_store, "JOIN", O_C)
    jf = shop_facts.joins["orders->customers"]
    assert j.status is Status.OBSERVED
    assert j.value["match_rate"] == pytest.approx(jf.key_match_rate)
    assert j.value["row_match_rate"] == pytest.approx(jf.row_match_rate)
    assert j.value["coverage"] == pytest.approx(jf.coverage)
    assert j.value["cardinality"] == "N:1" and j.value["verdict"] == "OK"
    pk = one(shop_store, "PRIMARY_KEY", ORD)
    assert pk.provenance is Provenance.MEASURED and pk.value["unique"] is True
    created = one(shop_store, "TYPE_NOTE", col("orders", "created_at"))
    assert created.value["rate"] == 1.0 and created.value["holds"] is True
    # one measure batch per table so the backend can fuse, plus the enum phase
    assert all(
        len({r.table_ref for r in b if not isinstance(r, KeyOverlapReq)}) <= 1
        for b in fake.measured
    )
    assert shop_store.ledger_total("measure", "queries", None) == len(all_requests(fake))


def test_enum_discovery_only_below_threshold(
    shop_store: SQLiteStore, conn: Any, shop_facts: ShopFacts
) -> None:
    fake = FakeBackend(conn)
    Scheduler(shop_store, fake, Config()).run([CUS, ORD])
    top = {r.column.column for r in all_requests(fake) if isinstance(r, TopValues)}
    assert "segment" in top and "status" in top
    assert "email" not in top and "customer_id" not in top  # distinct > enum_max_distinct
    seg = one(shop_store, "VALUES", col("customers", "segment"))
    assert {v["value"]: v["count"] for v in seg.value["observed"]} == shop_facts.segment_counts
    status = one(shop_store, "VALUES", col("orders", "status"))
    assert {v["value"]: v["count"] for v in status.value["observed"]} == shop_facts.status_counts
    assert facts(shop_store, "VALUES", col("customers", "email")) == []
    # a lower threshold drops a column from enum discovery
    fake2 = FakeBackend(conn)
    Scheduler(shop_store, fake2, Config(enum_max_distinct=3)).run([ORD])
    assert not any(
        isinstance(r, TopValues) and r.column.column == "status" for r in all_requests(fake2)
    )


def test_large_table_uses_system_sample(
    shop_store: SQLiteStore, conn: Any, shop_facts: ShopFacts
) -> None:
    fake = FakeBackend(conn)
    report = Scheduler(shop_store, fake, Config(sampling=LARGE_ORDERS)).run([ORD])
    assert report.sampled_tables == [ORD]
    prof = [r for r in all_requests(fake) if isinstance(r, ProfileColumns)]
    assert prof and all(r.sample is not None and r.sample.method == "system" for r in prof)
    nr = one(shop_store, "NULL_RATE", col("orders", "deleted"))
    assert nr.status is Status.ESTIMATED and nr.method.startswith("sampled:system:")  # type: ignore[union-attr]
    est = nr.value["estimate"]
    assert est["low"] <= shop_facts.null_rates["orders.deleted"] <= est["high"]
    # the row count of a large table comes from catalog stats, labelled CATALOG, never MEASURED
    rc = facts(shop_store, "ROW_COUNT", ORD)
    assert [f.provenance for f in rc] == [Provenance.CATALOG]
    assert rc[0].value["rows"] == shop_facts.row_counts["orders"]
    assert not any(isinstance(r, RowCount) for r in all_requests(fake))


def test_large_key_uses_keyhash_same_seed_both_sides(
    shop_store: SQLiteStore, conn: Any, shop_facts: ShopFacts
) -> None:
    cfg = Config(sampling=SamplingConfig(sample_threshold_rows=5000, exact_key_limit=500))
    fake = FakeBackend(conn)
    Scheduler(shop_store, fake, cfg).run([ORD])
    (req,) = [r for r in all_requests(fake) if isinstance(r, KeyOverlapReq) and r.join == O_C]
    assert req.sample is not None and req.sample.method == "keyhash"
    assert req.sample.key == ("customer_id",)
    j = one(shop_store, "JOIN", O_C)
    assert j.status is Status.ESTIMATED and j.method == req.sample.method_string()
    jf = shop_facts.joins["orders->customers"]
    # identical keys kept on both sides: every customer has an order, so every sampled
    # customer key is matched -- coverage stays exactly 1.0 only if both sides kept the same keys
    assert jf.coverage == 1.0 and j.value["coverage"] == 1.0
    assert j.value["match_rate"] == pytest.approx(jf.key_match_rate, abs=0.05)
    assert j.value["estimate"]["low"] < j.value["match_rate"] < j.value["estimate"]["high"]


def test_sampling_off_over_budget_is_error(shop_store: SQLiteStore, conn: Any) -> None:
    cfg = Config(
        sampling=SamplingConfig(measurement="off", sample_threshold_rows=5000),
        budget=Budget(max_bytes_per_probe=10_000),
    )
    fake = FakeBackend(conn)
    report = Scheduler(shop_store, fake, cfg).run([ORD])
    nr = one(shop_store, "NULL_RATE", col("orders", "status"))
    assert nr.status is Status.ERROR and nr.value["reason"] == "over_budget"
    assert nr.method == "exact"
    assert not any(isinstance(r, ProfileColumns) for r in all_requests(fake))  # never run
    assert {d.code for d in report.errors} == {"SG301"}
    assert report.sampled_tables == []


def test_probe_error_isolated(shop_store: SQLiteStore, conn: Any, shop_facts: ShopFacts) -> None:
    fake = FakeBackend(conn, fail=lambda r: isinstance(r, KeyOverlapReq) and r.join == O_A)
    report = Scheduler(shop_store, fake, Config()).run([ORD])
    err = one(shop_store, "JOIN", O_A)  # a declared-constraint join: its failure is recorded
    assert err.status is Status.ERROR and err.value["reason"] == "probe_error"
    assert one(shop_store, "JOIN", O_C).status is Status.OBSERVED
    assert one(shop_store, "NULL_RATE", col("orders", "deleted")).status is Status.OBSERVED
    assert [d.code for d in report.errors] == ["SG303"]
    assert report.measured_tables == [ORD]


def test_backend_crash_costs_one_table(shop_store: SQLiteStore, conn: Any) -> None:
    class Crashy(FakeBackend):
        def measure(self, reqs: Any) -> Any:
            if any(r.table_ref == CUS for r in reqs):
                raise RuntimeError("driver exploded")
            return super().measure(reqs)

    report = Scheduler(shop_store, Crashy(conn), Config()).run([CUS, PRD])
    assert one(shop_store, "ROW_COUNT", CUS).status is Status.ERROR
    assert one(shop_store, "ROW_COUNT", PRD).status is Status.OBSERVED
    assert "SG303" in {d.code for d in report.errors}


def test_budget_stops_and_reports_queue(shop_store: SQLiteStore, conn: Any) -> None:
    cfg = Config(budget=Budget(max_bytes_total=1_000, max_bytes_per_probe=None))
    fake = FakeBackend(conn, cost=lambda r: 400)
    s = Scheduler(shop_store, fake, cfg)
    report = s.run([PRD, CUS, ORD])
    assert report.over_budget  # the run stopped
    assert report.bytes <= 1_000
    assert report.over_budget[-1] == ORD
    assert [d.code for d in report.errors if d.code == "SG302"] == ["SG302"]
    measured = set(report.measured_tables)
    assert not (measured & set(report.over_budget) - {report.over_budget[0]})
    # draining the queue re-queues what did not fit
    for t in (PRD, CUS, ORD):
        shop_store.queue_push(t, "first_touch", 1.0)
    report = s.run(drain_queue=True)
    left = {ref for ref, _ in shop_store.queue_pop(100)}
    assert left == set(report.over_budget) and left


def test_candidate_never_becomes_fact_unmeasured(tmp_path: Path, conn: Any) -> None:
    with SQLiteStore(tmp_path / "live.db") as store:
        crawl(DuckDBReader(DBAPIExecutor(conn)), store)
        joins = lambda: store.facts_for(None, ["JOIN"])  # noqa: E731
        # a heuristic candidate that fails to measure leaves no JOIN fact at all
        Scheduler(
            store, FakeBackend(conn, fail=lambda r: isinstance(r, KeyOverlapReq)), Config()
        ).run([ORD])
        assert joins() == []
        Scheduler(
            store,
            FakeBackend(conn, cost=lambda r: 10**12 if isinstance(r, KeyOverlapReq) else 1),
            Config(),
        ).run([ORD])
        assert joins() == []
        # measured, it becomes a fact
        Scheduler(store, FakeBackend(conn), Config()).run([ORD])
        got = {f.subject: f for f in joins()}
        assert O_C in got and got[O_C].status is Status.OBSERVED


def test_run_all_tables_by_default(shop_store: SQLiteStore, conn: Any) -> None:
    report = Scheduler(shop_store, FakeBackend(conn), Config()).run()
    # big_t is not in the shop DuckDB connection: its probes fail, isolated to big_t
    assert ObjectRef(None, "shop", "big_t") in report.measured_tables
    assert one(shop_store, "ROW_COUNT", ObjectRef(None, "shop", "big_t")).status is Status.ERROR
    assert one(shop_store, "ROW_COUNT", ObjectRef(None, "shop", "empty_t")).value["rows"] == 0
    dead = one(shop_store, "NULL_RATE", col("all_null_t", "c"))
    assert dead.value["dead"] is True and dead.value["rate"] == 1.0
    empty = one(shop_store, "NULL_RATE", col("empty_t", "v"))
    assert empty.status is Status.UNMEASURABLE and empty.value["rate"] is None


def test_unknown_table_reported(shop_store: SQLiteStore, conn: Any) -> None:
    report = Scheduler(shop_store, FakeBackend(conn), Config()).run([ObjectRef(None, "x", "y")])
    assert [d.code for d in report.errors] == ["SG305"] and report.measured_tables == []


def test_background_drainer(shop_store: SQLiteStore, conn: Any) -> None:
    with pytest.raises(ValueError):
        BackgroundDrainer(Scheduler(shop_store, FakeBackend(conn), Config()))
    s = Scheduler(shop_store, FakeBackend(conn), Config(measure_on_demand="background"))
    drainer = BackgroundDrainer(s, interval_s=0.01)
    drainer.start()
    try:
        assert drainer.is_alive()
        shop_store.queue_push(PRD, "first_touch", 1.0)
        deadline = time.monotonic() + 10
        while not facts(shop_store, "ROW_COUNT", PRD) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert one(shop_store, "ROW_COUNT", PRD).value["rows"] == 200
    finally:
        drainer.stop()
    assert not drainer.is_alive()


def test_measure_codes_registered() -> None:
    from schemaguru.diagnostics import CODES

    doc = (Path(__file__).resolve().parents[3] / "docs/reference/diagnostics.md").read_text(
        encoding="utf-8"
    )
    for code in ("SG301", "SG302", "SG303", "SG304", "SG305"):
        assert code in CODES and f"| {code} |" in doc


def test_stop_in_enum_phase_lists_table_once(shop_store: SQLiteStore, conn: Any) -> None:
    # orders' enum phase runs TopValues(status), then TopValues(deleted) exhausts the budget
    def cost(r: Request) -> int:
        if isinstance(r, TopValues):
            return 60_000 if r.column.column == "deleted" else 50_000
        return 1

    cfg = Config(budget=Budget(max_bytes_total=10**5, max_bytes_per_probe=None))
    report = Scheduler(shop_store, FakeBackend(conn, cost=cost), cfg).run([ORD, PRD])
    assert report.measured_tables == [ORD] and report.over_budget == [ORD, PRD]
    assert one(shop_store, "VALUES", col("orders", "status")).status is Status.OBSERVED


def test_one_phase_cannot_exceed_total(shop_store: SQLiteStore, conn: Any) -> None:
    # every probe of orders costs 400; the whole phase would be admitted before measuring
    cfg = Config(budget=Budget(max_bytes_total=1_000, max_bytes_per_probe=None))
    fake = FakeBackend(conn, cost=lambda r: 400)
    report = Scheduler(shop_store, fake, cfg).run([ORD])
    assert len(all_requests(fake)) == 2  # 2 x 400 fit in 1000, the third would not
    assert report.bytes <= 1_000 and report.over_budget == [ORD]
    assert shop_store.ledger_total("measure", "bytes", None) <= 1_000


def test_single_probe_over_total_is_sg301_and_leaves_queue(shop_store: SQLiteStore,
                                                           conn: Any) -> None:  # fmt: skip
    cfg = Config(budget=Budget(max_bytes_total=100, max_bytes_per_probe=None))
    fake = FakeBackend(conn, cost=lambda r: 10**6 if isinstance(r, KeyOverlapReq) else 1)
    shop_store.queue_push(ORD, "first_touch", 1.0)
    report = Scheduler(shop_store, fake, cfg).run(drain_queue=True)
    assert "SG302" not in {d.code for d in report.errors}
    assert "SG301" in {d.code for d in report.errors}
    assert report.over_budget == [] and shop_store.queue_pop(10) == []
    assert one(shop_store, "JOIN", O_C).value["reason"] == "over_budget"


def test_crash_requeues_unmeasured_tables(shop_store: SQLiteStore, conn: Any) -> None:
    class Flaky:
        """Delegates to the store; the second put_facts raises."""

        def __init__(self, inner: SQLiteStore) -> None:
            self.inner, self.calls = inner, 0

        def put_facts(self, facts: Any) -> None:
            self.calls += 1
            if self.calls == 2:
                raise OSError("disk full")
            self.inner.put_facts(facts)

        def __getattr__(self, name: str) -> Any:
            return getattr(self.inner, name)

    for t in (PRD, CUS, ORD):
        shop_store.queue_push(t, "first_touch", 1.0)
    s = Scheduler(Flaky(shop_store), FakeBackend(conn), Config())  # type: ignore[arg-type]
    with pytest.raises(OSError):
        s.run(drain_queue=True)
    left = {ref: reason for ref, reason in shop_store.queue_pop(100)}
    assert set(left) == {CUS, ORD} and left[CUS] == "first_touch"  # PRD was written


def test_unknown_cost_visible_in_report(shop_store: SQLiteStore, conn: Any) -> None:
    from dataclasses import replace

    prd = shop_store.table(PRD)
    assert prd is not None  # no row count either: nothing to price from (see the next test)
    shop_store.upsert_tables([replace(prd, row_count=None, bytes=None)])
    report = Scheduler(shop_store, FakeBackend(conn, cost=lambda r: None), Config()).run([PRD])
    assert report.unknown_cost_probes > 0 and report.unknown_spend_probes > 0
    assert shop_store.ledger_total("measure.unknown_bytes", "queries", None) > 0


def test_drainer_shares_budget_and_backs_off(shop_store: SQLiteStore, conn: Any) -> None:
    cfg = Config(measure_on_demand="background",
                 budget=Budget(max_bytes_total=1_000, max_bytes_per_probe=None))  # fmt: skip
    fake = FakeBackend(conn, cost=lambda r: 300)
    d = BackgroundDrainer(Scheduler(shop_store, fake, cfg), interval_s=1.0)
    spent = 0
    for _ in range(4):
        shop_store.queue_push(PRD, "first_touch", 1.0)
        shop_store.queue_push(CUS, "first_touch", 1.0)
        r = d.run_once()
        spent += r.bytes
    assert spent <= 1_000  # one budget across iterations, not 1_000 per loop
    assert shop_store.ledger_total("measure", "bytes", None) <= 1_000
    assert d.wait_s > 1.0  # backing off after SG302
    assert d.wait_s <= 1.0 * BackgroundDrainer.MAX_BACKOFF


def test_large_key_distinct_comes_from_keyhash(shop_store: SQLiteStore, conn: Any) -> None:
    cfg = Config(sampling=SamplingConfig(sample_threshold_rows=5000, exact_key_limit=500))
    fake = FakeBackend(conn)
    Scheduler(shop_store, fake, cfg).run([ORD])
    (u,) = [r for r in all_requests(fake) if isinstance(r, Uniqueness)]
    assert u.sample is not None and u.sample.method == "keyhash" and u.sample.key == ("order_id",)
    d = one(shop_store, "DISTINCT", col("orders", "order_id"))
    assert d.status is Status.ESTIMATED and d.value["estimate"]["method"] == "scaled"
    assert d.value["distinct_keys"] == pytest.approx(10_000, rel=0.25)
    pk = one(shop_store, "PRIMARY_KEY", ORD)
    assert pk.value["row_count"] == pytest.approx(10_000, rel=0.25)


def test_drainer_backoff_resets_after_clean_run(shop_store: SQLiteStore, conn: Any) -> None:
    budget = Budget(max_bytes_total=1_000, max_bytes_per_probe=None, period_s=None)
    cfg = Config(measure_on_demand="background", budget=budget)
    d = BackgroundDrainer(Scheduler(shop_store, FakeBackend(conn, cost=lambda r: 300), cfg),
                          interval_s=1.0)  # fmt: skip
    for _ in range(3):
        shop_store.queue_push(CUS, "first_touch", 1.0)
        d.run_once()
    assert d.wait_s > 1.0
    shop_store.queue_pop(100)
    clean = d.run_once()  # nothing queued: a clean run
    assert not any(e.code == "SG302" for e in clean.errors) and d.wait_s == 1.0


def test_drainer_refills_per_period(shop_store: SQLiteStore, conn: Any) -> None:
    now = [0.0]
    budget = Budget(max_bytes_total=1_000, max_bytes_per_probe=None, period_s=60.0)
    cfg = Config(measure_on_demand="background", budget=budget)
    d = BackgroundDrainer(Scheduler(shop_store, FakeBackend(conn, cost=lambda r: 400), cfg),
                          interval_s=1.0, clock=lambda: now[0])  # fmt: skip
    shop_store.queue_push(CUS, "first_touch", 1.0)
    first = d.run_once()
    assert first.over_budget == [CUS]
    shop_store.queue_push(CUS, "first_touch", 1.0)
    assert d.run_once().bytes == 0  # same period: nothing left
    now[0] = 61.0
    assert d.run_once().bytes > 0  # refilled


def test_large_table_top_values_use_keyhash_on_the_primary_key(
    shop_store: SQLiteStore, conn: Any
) -> None:
    fake = FakeBackend(conn)
    Scheduler(shop_store, fake, Config(sampling=LARGE_ORDERS)).run([ORD])
    tops = [r for r in all_requests(fake) if isinstance(r, TopValues)]
    assert tops, "status is an enum candidate"
    for r in tops:
        assert r.sample is not None and r.sample.method == "keyhash"
        assert r.sample.key == ("order_id",)
    status = one(shop_store, "VALUES", col("orders", "status"))
    assert status.status is Status.ESTIMATED and status.value["seen_in_sample"] is True
    assert "approximate" not in status.value


def test_fake_backend_refuses_block_or_row_samples_for_value_membership_and_overlap(
    conn: Any,
) -> None:
    from decimal import Decimal

    from schemaguru.measure.requests import SampleChoice

    fake = FakeBackend(conn)
    for method in ("system", "bernoulli"):
        s = SampleChoice(method, Decimal("0.5"), 1)  # type: ignore[arg-type]
        reqs = [TopValues(col("orders", "status"), 5, sample=s), KeyOverlapReq(O_C, sample=s)]
        for r in fake.measure(reqs):
            assert r.error is not None and "FL405" in r.error.message
    ok = fake.measure(
        [ProfileColumns(ORD, ("status",), sample=SampleChoice("system", Decimal("0.5"), 1))]
    )
    assert ok[0].error is None


def test_unknown_cost_falls_back_to_a_row_count_heuristic(
    conn: Any, shop_store: SQLiteStore
) -> None:
    """No dry-run and no catalog bytes (DuckDB): ``rows x columns x 8`` prices the probe, so a
    small table is not charged the pessimistic unknown-cost price (which would admit only 10
    probes per default budget); with no row count either it stays unknown."""
    from dataclasses import replace

    from schemaguru.measure.requests import RowCount

    sched = Scheduler(shop_store, FakeBackend(conn, cost=lambda r: None), Config())
    orders = shop_store.table(ORD)
    assert orders is not None and orders.bytes is None and orders.row_count
    ncols = len(shop_store.columns(ORD))
    est = sched._estimate(RowCount(ORD), orders)
    assert est.method == "heuristic" and est.bytes == orders.row_count * ncols * 8
    unknown = sched._estimate(RowCount(ORD), replace(orders, row_count=None))
    assert unknown.bytes is None and unknown.method == "unknown"
    report = sched.run([ORD])
    assert report.unknown_cost_probes == 0
