"""The shop warehouse's knowledge as facts, for rendering tests (no warehouse needed).

Structure mirrors the shop DDL (``tests/fixtures/shop/build.py``); numbers are the fixture's exact
answers (``EXPECTED.md``). Facts of every provenance appear: CATALOG stats, MEASURED (exact and
sampled), DRAFTED (unverified, verified and failed), HUMAN overlays. Everything is stamped with
fixed times, so a view rendered at ``NOW`` is byte-deterministic.
"""

from __future__ import annotations

import random
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from schemaguru.config import Config
from schemaguru.model.evidence import Evidence
from schemaguru.model.facts import Fact, Provenance, Status
from schemaguru.model.joins import JOIN_VALUE_KEYS, JoinRef
from schemaguru.model.kinds import FactKind, KindSpec
from schemaguru.model.refs import ObjectRef
from schemaguru.overlays.schema import GLOBAL_SUBJECT
from schemaguru.store.sqlite import SQLiteStore
from tests.fixtures.shop.build import (
    EVENT_ACCOUNTS,
    N_ACCOUNTS,
    N_ALL_NULL,
    N_ANON,
    N_BIG,
    N_BROKEN,
    N_BROKEN_MATCH,
    N_CUSTOMERS,
    N_DELETED,
    N_EVENTS,
    N_ORDERS,
    N_ORPHANS,
    N_PAID_UPPER,
    N_PAYMENTS,
    N_PRODUCTS,
    N_REF_CHILD,
    N_REF_PARENT,
    N_SESSIONS,
    ORDER_ACCOUNTS,
    SPECS,
    shop_catalog,
)

NOW = datetime(2026, 11, 24, 12, tzinfo=timezone.utc)
MEASURED_AT = datetime(2026, 11, 20, 9, 30, tzinfo=timezone.utc)
DRAFTED_AT = datetime(2026, 11, 19, 8, tzinfo=timezone.utc)
OLD = datetime(2026, 10, 1, 6, tzinfo=timezone.utc)  # older than the 14-day TTL at NOW
SAMPLE = "sampled:system:0.01:42"
CONFIG = Config()

ROWS: dict[str, int] = {
    "customers": N_CUSTOMERS,
    "accounts": N_ACCOUNTS,
    "orders": N_ORDERS,
    "products": N_PRODUCTS,
    "order_items": 2 * N_ORDERS,
    "payments": N_PAYMENTS,
    "sessions": N_SESSIONS,
    "events": N_EVENTS,
    "ref_parent": N_REF_PARENT,
    "ref_child": N_REF_CHILD,
    "broken_child": N_BROKEN,
    "empty_t": 0,
    "all_null_t": N_ALL_NULL,
    "anon_enum_t": N_ANON,
    "big_t": N_BIG,
}

QUESTIONS = (
    "revenue by customer segment from paid orders",
    "how many events per account and session",
    "which products appear in refunded order items",
)


def t(name: str) -> ObjectRef:
    return ObjectRef(None, "shop", name)


def c(table: str, column: str) -> ObjectRef:
    return ObjectRef(None, "shop", table, column)


def j(left: str, right: str) -> JoinRef:
    lt, lc = left.split(".")
    rt, rc = right.split(".")
    return JoinRef((c(lt, lc),), (c(rt, rc),))


def measured(
    kind: KindSpec,
    subject: ObjectRef | JoinRef,
    value: dict[str, Any],
    *,
    at: datetime = MEASURED_AT,
) -> Fact:
    return Fact.make(
        kind,
        subject,
        value,
        Provenance.MEASURED,
        Status.OBSERVED,
        method="exact",
        source="measure",
        observed_at=at,
    )


def sampled(kind: KindSpec, subject: ObjectRef, value: dict[str, Any]) -> Fact:
    return Fact.make(
        kind,
        subject,
        {**value, "fraction": "0.01", "approximate": True},
        Provenance.MEASURED,
        Status.ESTIMATED,
        method=SAMPLE,
        source="measure",
        observed_at=MEASURED_AT,
    )


def drafted(
    kind: KindSpec,
    subject: ObjectRef | JoinRef,
    value: dict[str, Any],
    status: Status = Status.UNVERIFIED,
) -> Fact:
    extra: dict[str, Any] = {}
    if status is Status.VERIFIED:
        extra = {
            "method": "exact",
            "evidence": Evidence(claim_text="drafted claim", verdict="PASS"),
        }
    return Fact.make(
        kind,
        subject,
        value,
        Provenance.DRAFTED,
        status,
        source="llm:test-model",
        observed_at=DRAFTED_AT,
        **extra,
    )


def human(kind: KindSpec, subject: ObjectRef | JoinRef, value: dict[str, Any]) -> Fact:
    return Fact.make(
        kind, subject, value, Provenance.HUMAN, Status.UNVERIFIED, source="overlay:schemaguru.yaml"
    )


def join_value(
    distinct_left: int,
    matched: int,
    distinct_right: int,
    left_non_null: int,
    matched_rows: int,
    max_left: int | None,
    max_right: int | None,
    cardinality: str | None,
    verdict: str,
) -> dict[str, Any]:
    v: dict[str, Any] = {k: None for k in JOIN_VALUE_KEYS}
    v.update(
        match_rate=matched / distinct_left,
        coverage=matched / distinct_right,
        row_match_rate=matched_rows / left_non_null,
        distinct_left=distinct_left,
        distinct_right=distinct_right,
        matched=matched,
        left_non_null=left_non_null,
        fan_in=left_non_null / distinct_left,
        max_left_per_key=max_left,
        max_right_per_key=max_right,
        cardinality=cardinality,
        verdict=verdict,
    )
    return v


def shop_facts() -> list[Fact]:
    """Every fact of the shop knowledge base, in a fixed order."""
    F = FactKind
    out: list[Fact] = []
    for name, rows in ROWS.items():
        if name in ("products", "big_t"):
            continue
        out.append(measured(F.ROW_COUNT, t(name), {"rows": rows}))
    out.append(measured(F.ROW_COUNT, t("products"), {"rows": N_PRODUCTS}, at=OLD))  # stale
    out.append(
        Fact.make(
            F.ROW_COUNT,
            t("big_t"),
            {"rows": N_BIG},
            Provenance.CATALOG,
            Status.OBSERVED,
            source="catalog_stats",
            observed_at=MEASURED_AT,
        )
    )

    # ---- orders ----------------------------------------------------------------------
    out += [
        drafted(F.DESCRIPTION, t("orders"), {"text": "One row per order."}),
        drafted(
            F.USE_CASES, t("orders"), {"questions": ["revenue", "order volume", "basket size"]}
        ),
        drafted(F.ENTITY_TYPE, t("orders"), {"entity": "order", "key": ["order_id"]}),
        drafted(F.DESCRIPTION, c("orders", "status"), {"text": "Order lifecycle state."}),
        measured(
            F.PRIMARY_KEY,
            t("orders"),
            {
                "columns": ["order_id"],
                "unique": True,
                "row_count": N_ORDERS,
                "non_null": N_ORDERS,
                "distinct_keys": N_ORDERS,
                "duplicate_keys": 0,
                "max_rows_per_key": 1,
                "complete": True,
            },
        ),
        human(
            F.VALUES,
            c("orders", "status"),
            {"values": ["paid", "refunded", "cancelled"], "case_sensitive": True, "split": None},
        ),
        measured(
            F.VALUES,
            c("orders", "status"),
            {
                "case_sensitive": True,
                "split": None,
                "distinct": 4,
                "non_null": N_ORDERS,
                "observed": [
                    {"value": "paid", "count": 5000},
                    {"value": "refunded", "count": 2588},
                    {"value": "cancelled", "count": 2000},
                    {"value": "PAID", "count": N_PAID_UPPER},
                ],
            },
        ),
        measured(
            F.TYPE_NOTE,
            c("orders", "created_at"),
            {
                "stored_as": "string",
                "means": "ISO-8601 timestamp",
                "pattern": "____-__-__T%Z",
                "matching": N_ORDERS,
                "non_null": N_ORDERS,
                "rate": 1.0,
                "holds": True,
            },
        ),
        drafted(
            F.SOFT_DELETE,
            t("orders"),
            {"predicate": "deleted IS NULL OR NOT deleted", "marker_columns": ["deleted"]},
            Status.VERIFIED,
        ),
        measured(
            F.NULL_RATE,
            c("orders", "deleted"),
            {
                "rate": (N_ORDERS - N_DELETED) / N_ORDERS,
                "dead": False,
                "rows": N_ORDERS,
                "non_null": N_DELETED,
            },
        ),
        measured(
            F.VALUES,
            c("orders", "deleted"),
            {
                "case_sensitive": True,
                "split": None,
                "distinct": 1,
                "non_null": N_DELETED,
                "observed": [{"value": True, "count": N_DELETED}],
            },
        ),
        human(
            F.PITFALL,
            t("orders"),
            {
                "rule": "a refund is a second payment row with a negative amount",
                "fix": "SUM(amount) nets refunds; count paid orders by status instead",
                "severity": "critical",
            },
        ),
        measured(
            F.JOIN,
            j("orders.customer_id", "customers.customer_id"),
            join_value(
                N_CUSTOMERS + N_ORPHANS,
                N_CUSTOMERS,
                N_CUSTOMERS,
                N_ORDERS,
                N_ORDERS - N_ORPHANS,
                22,
                1,
                "N:1",
                "OK",
            ),
        ),
        measured(
            F.JOIN,
            j("orders.account_id", "accounts.account_id"),
            join_value(
                ORDER_ACCOUNTS, ORDER_ACCOUNTS, N_ACCOUNTS, N_ORDERS, N_ORDERS, 250, 1, "N:1", "OK"
            ),
        ),
    ]

    # ---- customers -------------------------------------------------------------------
    out += [
        human(F.DESCRIPTION, t("customers"), {"text": "One row per customer account holder."}),
        human(F.USE_CASES, t("customers"), {"questions": ["customer segments", "retention"]}),
        measured(F.PII, c("customers", "email"), {"category": "email", "detector": "email"}),
        measured(
            F.NULL_RATE,
            c("customers", "phone"),
            {"rate": 0.1, "dead": False, "rows": N_CUSTOMERS, "non_null": 900},
        ),
        measured(F.DISTINCT, c("customers", "email"), {"distinct_keys": N_CUSTOMERS}),
        measured(
            F.VALUES,
            c("customers", "segment"),
            {
                "case_sensitive": True,
                "split": None,
                "distinct": 3,
                "non_null": N_CUSTOMERS,
                "observed": [
                    {"value": "retail", "count": 334},
                    {"value": "wholesale", "count": 333},
                    {"value": "vip", "count": 333},
                ],
            },
        ),
        measured(
            F.PRIMARY_KEY,
            t("customers"),
            {
                "columns": ["customer_id"],
                "unique": True,
                "row_count": N_CUSTOMERS,
                "non_null": N_CUSTOMERS,
                "distinct_keys": N_CUSTOMERS,
                "duplicate_keys": 0,
                "max_rows_per_key": 1,
                "complete": True,
            },
        ),
    ]

    # ---- order_items / products / payments -------------------------------------------
    out += [
        drafted(F.DESCRIPTION, t("order_items"), {"text": "One row per order line."}),
        measured(
            F.PRIMARY_KEY,
            t("order_items"),
            {
                "columns": ["order_id", "line_no"],
                "unique": True,
                "row_count": 2 * N_ORDERS,
                "non_null": 2 * N_ORDERS,
                "distinct_keys": 2 * N_ORDERS,
                "duplicate_keys": 0,
                "max_rows_per_key": 1,
                "complete": True,
            },
        ),
        measured(
            F.JOIN,
            j("order_items.order_id", "orders.order_id"),
            join_value(N_ORDERS, N_ORDERS, N_ORDERS, 2 * N_ORDERS, 2 * N_ORDERS, 2, 1, "N:1", "OK"),
        ),
        measured(
            F.JOIN,
            j("order_items.product_id", "products.product_id"),
            join_value(
                N_PRODUCTS, N_PRODUCTS, N_PRODUCTS, 2 * N_ORDERS, 2 * N_ORDERS, 200, 1, "N:1", "OK"
            ),
        ),
        drafted(F.DESCRIPTION, t("products"), {"text": "Product catalogue."}),
        human(F.DESCRIPTION, t("payments"), {"text": "One row per payment."}),
        measured(
            F.JOIN,
            j("payments.order_id", "orders.order_id"),
            join_value(N_PAYMENTS, N_PAYMENTS, N_ORDERS, N_PAYMENTS, N_PAYMENTS, 1, 1, "1:1", "OK"),
        ),
    ]

    # ---- events (large: sampled), sessions, accounts ---------------------------------
    out += [
        drafted(F.DESCRIPTION, t("events"), {"text": "One row per tracked user event."}),
        sampled(
            F.NULL_RATE,
            c("events", "account_id"),
            {
                "rate": 0.0,
                "dead": False,
                "sampled_rows": 200,
                "estimate": {
                    "value": 0.0,
                    "low": 0.0,
                    "high": 0.03713,
                    "method": "wilson95",
                    "lower_bound": False,
                    "approximate": True,
                },
            },
        ),
        sampled(
            F.DISTINCT,
            c("events", "session_id"),
            {
                "distinct_keys": 190,
                "sampled_rows": 200,
                "estimate": {
                    "value": 190,
                    "low": 190,
                    "high": None,
                    "method": "sample_lower_bound",
                    "lower_bound": True,
                    "approximate": True,
                },
            },
        ),
        sampled(
            F.VALUES,
            c("events", "kind"),
            {
                "case_sensitive": True,
                "split": None,
                "distinct": 3,
                "non_null": 200,
                "seen_in_sample": True,
                "complete": False,
                "sampled_rows": 200,
                "observed": [
                    {"value": "buy", "count": 67},
                    {"value": "click", "count": 67},
                    {"value": "view", "count": 66},
                ],
            },
        ),
        measured(
            F.JOIN,
            j("events.session_id", "sessions.session_id"),
            join_value(N_SESSIONS, N_SESSIONS, N_SESSIONS, N_EVENTS, N_EVENTS, 4, 1, "N:1", "OK"),
        ),
        measured(
            F.JOIN,
            j("events.account_id", "accounts.account_id"),
            join_value(
                EVENT_ACCOUNTS, EVENT_ACCOUNTS, N_ACCOUNTS, N_EVENTS, N_EVENTS, 334, 1, "N:1", "OK"
            ),
        ),
        measured(
            F.JOIN,
            j("events.session_id", "orders.order_id"),
            join_value(N_SESSIONS, 0, N_ORDERS, N_EVENTS, 0, 4, None, None, "BROKEN"),
        ),
        human(F.ENTITY_TYPE, t("accounts"), {"entity": "account"}),
        human(F.DESCRIPTION, t("accounts"), {"text": "One row per business account."}),
        human(F.TYPE_NOTE, c("sessions", "started_at"), {"stored_as": None, "means": "UTC start"}),
    ]

    # ---- the rest --------------------------------------------------------------------
    out += [
        measured(
            F.JOIN,
            j("ref_child.parent_id", "ref_parent.id"),
            join_value(3, 3, N_REF_PARENT, N_REF_CHILD, N_REF_CHILD, 1000, 1, "N:1", "OK"),
        ),
        measured(
            F.JOIN,
            j("broken_child.parent_id", "ref_parent.id"),
            join_value(
                N_BROKEN,
                N_BROKEN_MATCH,
                N_REF_PARENT,
                N_BROKEN,
                N_BROKEN_MATCH,
                1,
                1,
                "1:1",
                "BROKEN",
            ),
        ),
        human(F.NEVER_QUERY, t("broken_child"), {"reason": "abandoned migration copy"}),
        measured(
            F.NULL_RATE,
            c("all_null_t", "c"),
            {"rate": 1.0, "dead": True, "rows": N_ALL_NULL, "non_null": 0},
        ),
        Fact.make(
            F.ROW_COUNT,
            t("empty_t"),
            {"rows": None, "reason": "empty_table"},
            Provenance.MEASURED,
            Status.UNMEASURABLE,
            method="exact",
            observed_at=MEASURED_AT,
        ),
        human(
            F.GLOSSARY,
            GLOBAL_SUBJECT,
            {
                "term": "revenue",
                "definition": "sum of payments.amount over paid orders",
                "maps_to": [t("orders").to_dict(), t("payments").to_dict()],
            },
        ),
        human(
            F.PITFALL,
            GLOBAL_SUBJECT,
            {"rule": "timestamps stored as text are UTC", "fix": None, "severity": "warn"},
        ),
    ]
    return out


@contextmanager
def shop_store(
    path: Path, facts: Sequence[Fact] | None = None, *, shuffle: int | None = None
) -> Iterator[SQLiteStore]:
    """A store holding the shop catalog and ``facts`` (default :func:`shop_facts`).

    ``shuffle`` inserts tables, columns and facts in a seeded random order (determinism tests).
    """
    schemas, tables, columns, cons = shop_catalog(SimpleNamespace(row_counts=ROWS))
    rows = list(facts if facts is not None else shop_facts())
    order = list(tables)
    rng = random.Random(shuffle)
    if shuffle is not None:
        rng.shuffle(order)
        rng.shuffle(rows)
    with SQLiteStore(path) as store:
        store.upsert_schemas(schemas)
        store.upsert_tables(order)
        for tbl in order:
            store.upsert_columns(tbl.ref, columns[tbl.ref])
            table_cons = list(cons[tbl.ref])
            if shuffle is not None:
                rng.shuffle(table_cons)
            store.upsert_constraints(tbl.ref, table_cons)
        if shuffle is None:
            store.put_facts(rows)
        else:
            for f in rows:
                store.put_facts([f])
        yield store


__all__ = [
    "CONFIG",
    "NOW",
    "QUESTIONS",
    "SPECS",
    "c",
    "j",
    "shop_facts",
    "shop_graph",
    "shop_store",
    "t",
]


def shop_graph(store: SQLiteStore) -> Any:
    """The measured join graph of ``store`` (spec 9.1, 9.4), resolved at ``NOW``."""
    from schemaguru.graph import Graph, Hubs, build_edges, entity_tables
    from schemaguru.knowledge.resolve import resolve

    eff = resolve(list(store.iter_facts()), now=NOW, ttl_days=CONFIG.fact_ttl_days)
    edges = build_edges(eff, cfg=CONFIG.graph)
    return Graph(edges, Hubs.from_edges(edges, entity_tables(eff), CONFIG.graph))
