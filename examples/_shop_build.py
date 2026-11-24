"""Deterministic shop warehouse builder.

``build_shop`` creates schema ``shop`` on a DuckDB or Postgres connection and returns
:class:`ShopFacts`, the exact expected answers for every trap. Only generic shop names and
reserved example domains are used.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

Dialect = Literal["duckdb", "postgres"]

SEED = 7
N_CUSTOMERS = 1000
N_ORDERS = 10_000
N_ORPHANS = 20
N_PAID_UPPER = 412
N_DELETED = 300
N_ACCOUNTS = 100
ORDER_ACCOUNTS = 40
EVENT_ACCOUNTS = 60
N_SESSIONS = 5000
N_EVENTS = 20_000
N_PRODUCTS = 200
N_PAYMENTS = 500
N_REF_PARENT = 1000
N_REF_CHILD = 3000
N_BROKEN = 1000
N_BROKEN_MATCH = 10
N_ALL_NULL = 100
N_ANON = 10_000
N_BIG = 2_000_000
_EPOCH = datetime(2025, 1, 1, tzinfo=timezone.utc)

# table -> (columns [(name, sql type, family)], primary key)
SPECS: dict[str, tuple[list[tuple[str, str, str]], tuple[str, ...]]] = {
    "customers": (
        [
            ("customer_id", "INTEGER", "integer"),
            ("email", "VARCHAR", "string"),
            ("phone", "VARCHAR", "string"),
            ("segment", "VARCHAR", "string"),
        ],
        ("customer_id",),
    ),
    "accounts": (
        [("account_id", "INTEGER", "integer"), ("name", "VARCHAR", "string")],
        ("account_id",),
    ),
    "orders": (
        [
            ("order_id", "INTEGER", "integer"),
            ("customer_id", "INTEGER", "integer"),
            ("account_id", "INTEGER", "integer"),
            ("status", "VARCHAR", "string"),
            ("created_at", "VARCHAR", "string"),
            ("deleted", "BOOLEAN", "boolean"),
        ],
        ("order_id",),
    ),
    "products": (
        [("product_id", "INTEGER", "integer"), ("name", "VARCHAR", "string")],
        ("product_id",),
    ),
    "order_items": (
        [
            ("order_id", "INTEGER", "integer"),
            ("line_no", "INTEGER", "integer"),
            ("product_id", "INTEGER", "integer"),
        ],
        ("order_id", "line_no"),
    ),
    "payments": (
        [
            ("payment_id", "INTEGER", "integer"),
            ("order_id", "INTEGER", "integer"),
            ("amount", "INTEGER", "integer"),
        ],
        ("payment_id",),
    ),
    "sessions": (
        [("session_id", "VARCHAR", "string"), ("started_at", "VARCHAR", "string")],
        ("session_id",),
    ),
    "events": (
        [
            ("event_id", "INTEGER", "integer"),
            ("session_id", "VARCHAR", "string"),
            ("account_id", "INTEGER", "integer"),
            ("kind", "VARCHAR", "string"),
        ],
        ("event_id",),
    ),
    "ref_parent": ([("id", "INTEGER", "integer")], ("id",)),
    "ref_child": (
        [("child_id", "INTEGER", "integer"), ("parent_id", "INTEGER", "integer")],
        ("child_id",),
    ),
    "broken_child": (
        [("child_id", "INTEGER", "integer"), ("parent_id", "INTEGER", "integer")],
        ("child_id",),
    ),
    "empty_t": ([("id", "INTEGER", "integer"), ("v", "VARCHAR", "string")], ()),
    "all_null_t": ([("id", "INTEGER", "integer"), ("c", "INTEGER", "integer")], ()),
    "anon_enum_t": ([("id", "INTEGER", "integer"), ("kind", "VARCHAR", "string")], ()),
}
BIG_SPEC = [("id", "INTEGER", "integer"), ("k", "INTEGER", "integer"), ("v", "INTEGER", "integer")]

# declared foreign keys (child table, columns, parent table, parent columns)
DECLARED_FKS: list[tuple[str, tuple[str, ...], str, tuple[str, ...]]] = [
    ("orders", ("customer_id",), "customers", ("customer_id",)),
    ("orders", ("account_id",), "accounts", ("account_id",)),
    ("order_items", ("order_id",), "orders", ("order_id",)),
    ("order_items", ("product_id",), "products", ("product_id",)),
    ("payments", ("order_id",), "orders", ("order_id",)),
    ("events", ("session_id",), "sessions", ("session_id",)),
    ("events", ("account_id",), "accounts", ("account_id",)),
    ("ref_child", ("parent_id",), "ref_parent", ("id",)),
    ("broken_child", ("parent_id",), "ref_parent", ("id",)),
]


@dataclass(frozen=True)
class JoinFacts:
    """Exact measurements of ``left -> right`` (distinct-key and row-level rates apart).

    ``key_match_rate`` = matched_keys / distinct_left; ``row_match_rate`` = matched_rows /
    left_non_null; ``coverage`` = matched_keys / right_distinct (reverse coverage).
    """

    left_non_null: int
    distinct_left: int
    matched_keys: int
    matched_rows: int
    right_distinct: int

    @property
    def key_match_rate(self) -> float:
        return self.matched_keys / self.distinct_left

    @property
    def row_match_rate(self) -> float:
        return self.matched_rows / self.left_non_null

    @property
    def coverage(self) -> float:
        return self.matched_keys / self.right_distinct


@dataclass(frozen=True)
class ShopFacts:
    row_counts: dict[str, int]
    joins: dict[str, JoinFacts]
    status_counts: dict[str, int]
    deleted_true: int
    deleted_null: int
    deleted_false: int
    null_counts: dict[str, int]
    null_rates: dict[str, float | None]
    distinct_counts: dict[str, int]
    segment_counts: dict[str, int]
    created_at_min: str
    created_at_max: str
    # joins whose names state the trap
    thin_but_correct: str = "payments->orders"
    broken: str = "broken_child->ref_parent"
    asymmetric: str = "ref_child->ref_parent"
    hub: str = "accounts"
    hub_fan_in: dict[str, int] = field(default_factory=dict)


def _lit(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, int):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def _insert(conn: Any, table: str, rows: list[tuple[Any, ...]], chunk: int = 2000) -> None:
    for i in range(0, len(rows), chunk):
        values = ",".join("(" + ",".join(_lit(v) for v in r) + ")" for r in rows[i : i + chunk])
        conn.execute(f"INSERT INTO shop.{table} VALUES {values}")


def _create(conn: Any, table: str, cols: list[tuple[str, str, str]], pk: tuple[str, ...]) -> None:
    parts = [f"{n} {t}" for n, t, _ in cols]
    if pk:
        parts.append(f"PRIMARY KEY ({', '.join(pk)})")
    conn.execute(f"CREATE TABLE shop.{table} ({', '.join(parts)})")


def _iso(i: int) -> str:
    return (_EPOCH + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _anon(i: int) -> str:
    return hashlib.md5(f"anon-{i}".encode()).hexdigest() + "@anon.example"  # 32 hex chars


def build_shop(conn: Any, dialect: Dialect = "duckdb") -> ShopFacts:
    """Create schema ``shop`` (without ``big_t``) and return the exact expected answers."""
    rng = random.Random(SEED)
    if dialect == "postgres":
        conn.execute("DROP SCHEMA IF EXISTS shop CASCADE")
    conn.execute("CREATE SCHEMA IF NOT EXISTS shop")
    for name, (cols, pk) in SPECS.items():
        _create(conn, name, cols, pk)

    segs = ["retail", "wholesale", "vip"]
    cust = [
        (
            i,
            f"user{i}@example.com",
            None if i % 10 == 0 else f"+1555{i:07d}",
            segs[(i - 1) % 3],
        )
        for i in range(1, N_CUSTOMERS + 1)
    ]
    _insert(conn, "customers", cust)
    _insert(conn, "accounts", [(i, f"account {i}") for i in range(1, N_ACCOUNTS + 1)])

    statuses = (
        ["paid"] * 5000 + ["refunded"] * 2588 + ["cancelled"] * 2000 + ["PAID"] * N_PAID_UPPER
    )
    rng.shuffle(statuses)
    cust_ids = [rng.randint(1, N_CUSTOMERS) for _ in range(N_ORDERS - N_ORPHANS)]
    cust_ids[:N_CUSTOMERS] = range(1, N_CUSTOMERS + 1)  # every customer has an order
    rng.shuffle(cust_ids)
    cust_ids += [100_000 + k for k in range(N_ORPHANS)]  # 20 distinct orphan keys
    rng.shuffle(cust_ids)
    deleted_at = set(rng.sample(range(1, N_ORDERS + 1), N_DELETED))
    acct = [1 + (i % ORDER_ACCOUNTS) for i in range(N_ORDERS)]
    orders = [
        (
            i,
            cust_ids[i - 1],
            acct[i - 1],
            statuses[i - 1],
            _iso(i),
            True if i in deleted_at else None,
        )
        for i in range(1, N_ORDERS + 1)
    ]
    _insert(conn, "orders", orders)
    _insert(conn, "products", [(i, f"product {i}") for i in range(1, N_PRODUCTS + 1)])
    items = [
        (o, ln, 1 + (o * 7 + ln * 13) % N_PRODUCTS) for o in range(1, N_ORDERS + 1) for ln in (1, 2)
    ]
    _insert(conn, "order_items", items)
    paid_orders = sorted(rng.sample(range(1, N_ORDERS + 1), N_PAYMENTS))
    _insert(conn, "payments", [(i + 1, o, 10 + i) for i, o in enumerate(paid_orders)])
    _insert(conn, "sessions", [(f"s{i}", _iso(i)) for i in range(1, N_SESSIONS + 1)])
    kinds = ["view", "click", "buy"]
    _insert(
        conn,
        "events",
        [
            (i, f"s{1 + (i * 3) % N_SESSIONS}", 1 + (i % EVENT_ACCOUNTS), kinds[i % 3])
            for i in range(1, N_EVENTS + 1)
        ],
    )
    _insert(conn, "ref_parent", [(i,) for i in range(1, N_REF_PARENT + 1)])
    _insert(conn, "ref_child", [(i, 1 + (i % 3)) for i in range(1, N_REF_CHILD + 1)])
    _insert(
        conn,
        "broken_child",
        [(i, i if i <= N_BROKEN_MATCH else 500_000 + i) for i in range(1, N_BROKEN + 1)],
    )
    _insert(conn, "all_null_t", [(i, None) for i in range(1, N_ALL_NULL + 1)])
    anon = [_anon(k) for k in range(5)]
    _insert(conn, "anon_enum_t", [(i, anon[i % 5]) for i in range(1, N_ANON + 1)])

    n_phone_null = sum(1 for c in cust if c[2] is None)
    n_status: dict[str, int] = {}
    for s in statuses:
        n_status[s] = n_status.get(s, 0) + 1
    seg_counts = {s: sum(1 for c in cust if c[3] == s) for s in segs}
    matched_rows = sum(1 for c in cust_ids if c <= N_CUSTOMERS)
    created = [o[4] for o in orders]
    rows = {n: 0 for n in SPECS}
    rows.update(
        customers=N_CUSTOMERS,
        accounts=N_ACCOUNTS,
        orders=N_ORDERS,
        products=N_PRODUCTS,
        order_items=len(items),
        payments=N_PAYMENTS,
        sessions=N_SESSIONS,
        events=N_EVENTS,
        ref_parent=N_REF_PARENT,
        ref_child=N_REF_CHILD,
        broken_child=N_BROKEN,
        empty_t=0,
        all_null_t=N_ALL_NULL,
        anon_enum_t=N_ANON,
    )
    rows["big_t"] = N_BIG
    joins = {
        "orders->customers": JoinFacts(
            N_ORDERS,
            len(set(cust_ids)),
            len({c for c in cust_ids if c <= N_CUSTOMERS}),
            matched_rows,
            N_CUSTOMERS,
        ),
        "orders->accounts": JoinFacts(
            N_ORDERS, ORDER_ACCOUNTS, ORDER_ACCOUNTS, N_ORDERS, N_ACCOUNTS
        ),
        "events->accounts": JoinFacts(
            N_EVENTS, EVENT_ACCOUNTS, EVENT_ACCOUNTS, N_EVENTS, N_ACCOUNTS
        ),
        "order_items->orders": JoinFacts(len(items), N_ORDERS, N_ORDERS, len(items), N_ORDERS),
        "order_items->products": JoinFacts(
            len(items),
            len({i[2] for i in items}),
            len({i[2] for i in items}),
            len(items),
            N_PRODUCTS,
        ),
        "payments->orders": JoinFacts(N_PAYMENTS, N_PAYMENTS, N_PAYMENTS, N_PAYMENTS, N_ORDERS),
        "events->sessions": JoinFacts(
            N_EVENTS,
            len({f"s{1 + (i * 3) % N_SESSIONS}" for i in range(1, N_EVENTS + 1)}),
            len({f"s{1 + (i * 3) % N_SESSIONS}" for i in range(1, N_EVENTS + 1)}),
            N_EVENTS,
            N_SESSIONS,
        ),
        "events->orders": JoinFacts(N_EVENTS, N_SESSIONS, 0, 0, N_ORDERS),
        "ref_child->ref_parent": JoinFacts(N_REF_CHILD, 3, 3, N_REF_CHILD, N_REF_PARENT),
        "broken_child->ref_parent": JoinFacts(
            N_BROKEN, N_BROKEN, N_BROKEN_MATCH, N_BROKEN_MATCH, N_REF_PARENT
        ),
    }
    return ShopFacts(
        row_counts=rows,
        joins=joins,
        status_counts=n_status,
        deleted_true=N_DELETED,
        deleted_null=N_ORDERS - N_DELETED,
        deleted_false=0,
        null_counts={
            "orders.deleted": N_ORDERS - N_DELETED,
            "customers.phone": n_phone_null,
            "all_null_t.c": N_ALL_NULL,
        },
        null_rates={
            "orders.deleted": (N_ORDERS - N_DELETED) / N_ORDERS,
            "customers.phone": n_phone_null / N_CUSTOMERS,
            "all_null_t.c": 1.0,
            "empty_t.v": None,  # unknown, never 0
        },
        distinct_counts={
            "customers.email": N_CUSTOMERS,
            "anon_enum_t.kind": 5,
            "orders.account_id": ORDER_ACCOUNTS,
            "events.account_id": EVENT_ACCOUNTS,
            "orders.status": len(n_status),
        },
        segment_counts=seg_counts,
        created_at_min=min(created),
        created_at_max=max(created),
        hub_fan_in={"orders.account_id": ORDER_ACCOUNTS, "events.account_id": EVENT_ACCOUNTS},
    )


def build_big(conn: Any, dialect: Dialect = "duckdb") -> int:
    """Create ``shop.big_t`` (2,000,000 rows, set-based SQL) and return its row count."""
    if dialect == "postgres":
        conn.execute("CREATE SCHEMA IF NOT EXISTS shop")
        conn.execute("DROP TABLE IF EXISTS shop.big_t")
        series = f"generate_series(1, {N_BIG})"
    else:
        conn.execute("CREATE SCHEMA IF NOT EXISTS shop")
        conn.execute("DROP TABLE IF EXISTS shop.big_t")
        series = f"range(1, {N_BIG + 1})"
    col = "g" if dialect == "postgres" else "range"
    src = f"{series} AS t(g)" if dialect == "postgres" else series
    conn.execute(
        f"CREATE TABLE shop.big_t AS SELECT CAST({col} AS INTEGER) AS id, "
        f"CAST({col} % 1000 AS INTEGER) AS k, CAST({col} % 97 AS INTEGER) AS v FROM {src}"
    )
    return N_BIG


def shop_catalog(facts: ShopFacts) -> Any:
    """Structural objects mirroring the shop DDL (declared PKs and FKs), no crawler needed.

    Returns ``(schemas, tables, columns_by_table, constraints_by_table)``.
    """
    from schemaguru.model.refs import ObjectRef
    from schemaguru.model.structure import Column, Constraint, Schema, Table

    schemas = [Schema(None, "shop", None, len(SPECS) + 1)]
    specs = dict(SPECS)
    specs["big_t"] = (BIG_SPEC, ())
    tables: list[Table] = []
    columns: dict[ObjectRef, list[Column]] = {}
    cons: dict[ObjectRef, list[Constraint]] = {}
    for name, (cols, pk) in specs.items():
        ref = ObjectRef(None, "shop", name)
        tables.append(Table(ref, "table", None, facts.row_counts.get(name)))
        columns[ref] = [
            Column(ObjectRef(None, "shop", name, c), i, t, fam, nullable=c not in pk)
            for i, (c, t, fam) in enumerate(cols, 1)
        ]
        cons[ref] = [Constraint("primary_key", ref, pk, None, None, True)] if pk else []
    for child, ccols, parent, pcols in DECLARED_FKS:
        cref = ObjectRef(None, "shop", child)
        cons[cref].append(
            Constraint("foreign_key", cref, ccols, ObjectRef(None, "shop", parent), pcols, False)
        )
    return schemas, tables, columns, cons
