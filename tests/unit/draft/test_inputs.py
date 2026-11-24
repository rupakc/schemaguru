from __future__ import annotations

import json
from datetime import datetime, timezone

from schemaguru.config import Config, SamplingConfig
from schemaguru.draft.inputs import build_inputs
from schemaguru.model.facts import Fact, Provenance, Status
from schemaguru.model.kinds import FactKind
from schemaguru.model.refs import ObjectRef
from schemaguru.model.structure import Table
from schemaguru.store.sqlite import SQLiteStore

CUST = ObjectRef(None, "shop", "customers")
ORD = ObjectRef(None, "shop", "orders")
AT = datetime(2026, 11, 20, tzinfo=timezone.utc)
EMAILS = ["ann.lee@example.com", "bo@example.org"]


def _observed(store: SQLiteStore, col: ObjectRef, values: list[str]) -> None:
    store.put_facts(
        [
            Fact.make(
                FactKind.VALUES,
                col,
                {
                    "case_sensitive": True,
                    "split": None,
                    "observed": [{"value": v, "count": 3} for v in values],
                    "distinct": len(values),
                    "non_null": 3 * len(values),
                },
                Provenance.MEASURED,
                Status.OBSERVED,
                method="exact",
                observed_at=AT,
            )
        ]
    )


def test_masked_values_are_shape_tokens(shop_store: SQLiteStore) -> None:
    _observed(shop_store, ObjectRef(None, "shop", "customers", "email"), EMAILS)
    di = build_inputs(shop_store, CUST, Config())
    text = di.text()
    assert di.mode == "masked"
    assert "example.com" not in text and "example.org" not in text
    email = next(c for c in di.payload["columns"] if c["name"] == "email")
    assert [v["value"] for v in email["values"]] == ["<email>", "<email>"]
    assert ("email", "email") in di.masked


def test_examples_are_capped_and_masked(shop_store: SQLiteStore) -> None:
    ex = {"phone": ["+44 20 7946 0958"] * 9}
    di = build_inputs(shop_store, CUST, Config(), examples=ex)
    phone = next(c for c in di.payload["columns"] if c["name"] == "phone")
    assert phone["examples"] == ["<phone>"] * 5  # sample_values_per_column
    assert "7946" not in di.text()


def test_final_sweep_counts_hits_in_comments(shop_store: SQLiteStore) -> None:
    shop_store.upsert_tables([Table(CUST, "table", "owner: ops-team@example.com", 1000)])
    di = build_inputs(shop_store, CUST, Config())
    assert "ops-team@example.com" not in di.text()
    assert "<email>" in di.payload["comment"]
    assert di.pii_sweep_hits == 1


def test_raw_mode_keeps_values_and_skips_sweep(shop_store: SQLiteStore) -> None:
    _observed(shop_store, ObjectRef(None, "shop", "customers", "email"), EMAILS)
    cfg = Config(sampling=SamplingConfig(per_glob={"shop.customers": "raw"}))
    di = build_inputs(shop_store, CUST, cfg)
    assert di.mode == "raw"
    assert "ann.lee@example.com" in di.text()
    assert di.pii_sweep_hits == 0


def test_none_mode_has_no_values_but_keeps_statistics(shop_store: SQLiteStore) -> None:
    _observed(shop_store, ObjectRef(None, "shop", "orders", "status"), ["paid", "open"])
    shop_store.put_facts(
        [
            Fact.make(
                FactKind.NULL_RATE,
                ObjectRef(None, "shop", "orders", "status"),
                {"rate": 0.25, "dead": False},
                Provenance.MEASURED,
                Status.OBSERVED,
                method="exact",
                observed_at=AT,
            )
        ]
    )
    di = build_inputs(
        shop_store,
        ORD,
        Config(sampling=SamplingConfig(llm_values="none")),
        examples={"status": ["paid"]},
    )
    status = next(c for c in di.payload["columns"] if c["name"] == "status")
    assert "values" not in status and "examples" not in status
    assert status["null_rate"] == 0.25
    assert "paid" not in di.text()


def test_payload_carries_structure(shop_store: SQLiteStore) -> None:
    di = build_inputs(shop_store, ORD, Config())
    p = json.loads(di.text())
    assert p["table"] == "shop.orders"
    assert p["primary_key"] == ["order_id"]
    assert {
        "columns": ["customer_id"],
        "to_table": "shop.customers",
        "to_columns": ["customer_id"],
    } in p["foreign_keys"]
    assert "shop.customers" in p["neighbours"]
    assert [c["name"] for c in p["columns"]][:2] == ["order_id", "customer_id"]


def test_neighbours_include_join_facts(shop_store: SQLiteStore) -> None:
    from schemaguru.model.joins import JoinRef

    j = JoinRef(
        (ObjectRef(None, "shop", "events", "session_id"),),
        (ObjectRef(None, "shop", "sessions", "session_id"),),
    )
    shop_store.put_facts(
        [Fact.make(FactKind.JOIN, j, {"verdict": "OK"}, Provenance.HUMAN, Status.UNVERIFIED)]
    )
    events = build_inputs(shop_store, ObjectRef(None, "shop", "events"), Config())
    sessions = build_inputs(shop_store, ObjectRef(None, "shop", "sessions"), Config())
    assert "shop.sessions" in events.payload["neighbours"]
    assert "shop.events" in sessions.payload["neighbours"]
