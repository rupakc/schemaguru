"""Join facts with the shop fixture's exact numbers, resolved into Effective knowledge.

No warehouse: the numbers come from ``tests/fixtures/shop/EXPECTED.md`` / the ``JoinFacts`` the
fixture builder exposes (same constructor, same constants).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from schemaguru.knowledge.resolve import Effective, resolve
from schemaguru.model.facts import Fact, Provenance, Status
from schemaguru.model.joins import JOIN_VALUE_KEYS, JoinRef
from schemaguru.model.kinds import FactKind
from schemaguru.model.refs import ObjectRef
from tests.fixtures.shop import JoinFacts
from tests.fixtures.shop.build import (
    EVENT_ACCOUNTS,
    N_ACCOUNTS,
    N_BROKEN,
    N_BROKEN_MATCH,
    N_CUSTOMERS,
    N_EVENTS,
    N_ORDERS,
    N_ORPHANS,
    N_PAYMENTS,
    N_PRODUCTS,
    N_REF_CHILD,
    N_REF_PARENT,
    N_SESSIONS,
    ORDER_ACCOUNTS,
)

NOW = datetime(2026, 11, 24, 12, tzinfo=timezone.utc)


def t(name: str) -> ObjectRef:
    return ObjectRef(None, "shop", name)


def j(left: str, right: str) -> JoinRef:
    lt, lc = left.split(".")
    rt, rc = right.split(".")
    return JoinRef((ObjectRef(None, "shop", lt, lc),), (ObjectRef(None, "shop", rt, rc),))


# EXPECTED.md: orders->customers has 1,020 distinct keys (20 orphans), 9,980 matched rows.
SHOP_JOINS: dict[str, tuple[JoinRef, JoinFacts]] = {
    "orders->customers": (
        j("orders.customer_id", "customers.customer_id"),
        JoinFacts(
            N_ORDERS, N_CUSTOMERS + N_ORPHANS, N_CUSTOMERS, N_ORDERS - N_ORPHANS, N_CUSTOMERS
        ),
    ),
    "orders->accounts": (
        j("orders.account_id", "accounts.account_id"),
        JoinFacts(N_ORDERS, ORDER_ACCOUNTS, ORDER_ACCOUNTS, N_ORDERS, N_ACCOUNTS),
    ),
    "events->accounts": (
        j("events.account_id", "accounts.account_id"),
        JoinFacts(N_EVENTS, EVENT_ACCOUNTS, EVENT_ACCOUNTS, N_EVENTS, N_ACCOUNTS),
    ),
    "order_items->orders": (
        j("order_items.order_id", "orders.order_id"),
        JoinFacts(2 * N_ORDERS, N_ORDERS, N_ORDERS, 2 * N_ORDERS, N_ORDERS),
    ),
    "order_items->products": (
        j("order_items.product_id", "products.product_id"),
        JoinFacts(2 * N_ORDERS, N_PRODUCTS, N_PRODUCTS, 2 * N_ORDERS, N_PRODUCTS),
    ),
    "payments->orders": (
        j("payments.order_id", "orders.order_id"),
        JoinFacts(N_PAYMENTS, N_PAYMENTS, N_PAYMENTS, N_PAYMENTS, N_ORDERS),
    ),
    "events->sessions": (
        j("events.session_id", "sessions.session_id"),
        JoinFacts(N_EVENTS, N_SESSIONS, N_SESSIONS, N_EVENTS, N_SESSIONS),
    ),
    "events->orders": (
        j("events.session_id", "orders.order_id"),
        JoinFacts(N_EVENTS, N_SESSIONS, 0, 0, N_ORDERS),
    ),
    "ref_child->ref_parent": (
        j("ref_child.parent_id", "ref_parent.id"),
        JoinFacts(N_REF_CHILD, 3, 3, N_REF_CHILD, N_REF_PARENT),
    ),
    "broken_child->ref_parent": (
        j("broken_child.parent_id", "ref_parent.id"),
        JoinFacts(N_BROKEN, N_BROKEN, N_BROKEN_MATCH, N_BROKEN_MATCH, N_REF_PARENT),
    ),
}


def join_value(jf: JoinFacts, **over: Any) -> dict[str, Any]:
    v: dict[str, Any] = {k: None for k in JOIN_VALUE_KEYS}
    v.update(
        match_rate=jf.key_match_rate,
        coverage=jf.coverage,
        row_match_rate=jf.row_match_rate,
        distinct_left=jf.distinct_left,
        distinct_right=jf.right_distinct,
        matched=jf.matched_keys,
        left_non_null=jf.left_non_null,
        fan_in=jf.left_non_null / jf.distinct_left,
    )
    v.update(over)
    return v


def measured(join: JoinRef, value: Mapping[str, Any], *, sampled: bool = False) -> Fact:
    return Fact.make(
        FactKind.JOIN,
        join,
        dict(value),
        Provenance.MEASURED,
        Status.ESTIMATED if sampled else Status.OBSERVED,
        method="sampled:keyhash:0.1" if sampled else "exact",
        observed_at=NOW,
    )


def entity(table: ObjectRef) -> Fact:
    return Fact.make(
        FactKind.ENTITY_TYPE, table, {"entity": "account"}, Provenance.DRAFTED, Status.UNVERIFIED
    )


def shop_facts(skip: Sequence[str] = ()) -> list[Fact]:
    return [
        measured(jr, join_value(jf)) for name, (jr, jf) in SHOP_JOINS.items() if name not in skip
    ]


def effective(facts: Sequence[Fact]) -> dict[tuple[str, str], Effective]:
    return resolve(facts, now=NOW, ttl_days=30)
