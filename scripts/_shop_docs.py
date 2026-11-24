"""The shop fixture's knowledge, built the way a user would build it (docs generators only).

Shared by ``scripts/build_demo_export.py`` (the site demo's JSON) and ``scripts/gen_docs.py``
(the rendered outputs quoted on the documentation pages). Both must show *real* output, never
hand-written text, so both build the same thing here:

1. the shop warehouse (``tests/fixtures/shop``) as a DuckDB file;
2. a :class:`schemaguru.Guru` over it that crawls the catalog;
3. measurement through the test ``FakeBackend`` (it computes every payload with SQL on the
   same DuckDB file; FactLang's ``Checker.measure`` replaces it once FactLang 0.1.0 ships it —
   SchemaGuru itself never writes measurement SQL);
4. the spec 14.1 overlay (``tests/fixtures/overlays/spec_example.yaml``).

Everything is stamped with a fixed clock, so the outputs are byte-deterministic.

This module needs a repository checkout (it imports ``tests``); it is not part of the package.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import schemaguru as sg  # noqa: E402
from schemaguru.catalog import DBAPIExecutor, reader_for  # noqa: E402

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
OVERLAY = ROOT / "tests" / "fixtures" / "overlays" / "spec_example.yaml"
# the database file name must differ from the schema name: DuckDB names the catalog after the
# file, and a catalog ``shop`` holding a schema ``shop`` makes ``shop.orders`` ambiguous
DB_NAME = "warehouse.duckdb"

# the questions the demo precomputes, and the docs quote some of
QUESTIONS = (
    "monthly revenue by customer segment",
    "refund rate by month",
    "how many orders were paid last month",
    "events per account and session",
    "which products appear in refunded orders",
    "payments per customer",
    "average basket size per order",
    "active customers by segment",
    "how fresh are the sessions",
    "order line items per product",
)


@dataclass
class ShopGuru:
    guru: sg.Guru
    conn: Any  # read-only DuckDB connection (the FakeBackend's)
    facts: Any  # tests.fixtures.shop.ShopFacts
    db_path: Path
    before: sg.Guru  # crawled only: what SchemaGuru knows before measuring anything


def build_db(directory: Path) -> tuple[Path, Any]:
    """Build the shop warehouse in ``directory``; returns (path, ShopFacts)."""
    import duckdb
    from tests.fixtures.shop import build_shop

    path = directory / DB_NAME
    conn = duckdb.connect(str(path))
    try:
        facts = build_shop(conn, "duckdb")
    finally:
        conn.close()
    return path, facts


@contextmanager
def shop_guru(directory: Path) -> Iterator[ShopGuru]:
    """Build the shop, crawl, measure everything and load the overlay; yields a ShopGuru."""
    import duckdb
    from tests.unit.measure.fake_backend import FakeBackend

    path, facts = build_db(directory)
    conn = duckdb.connect(str(path), read_only=True)

    def clock() -> datetime:
        return NOW

    def make(store: str, *, backend: Any) -> sg.Guru:
        return sg.Guru(
            reader=reader_for("duckdb", DBAPIExecutor(conn)),
            backend=backend,
            store=str(directory / store),
            dialect="duckdb",
            name="shop",
            now=clock,
        )

    before = make("before.db", backend=None)
    guru = make("shop.db", backend=FakeBackend(conn))
    try:
        before.crawl()
        guru.crawl()
        guru.measure(all=True)
        guru.load_overlays(OVERLAY)
        # generated docs must not depend on optional FactLang capabilities: without a dialect
        # the renderer never asks FactLang for SQL join snippets (fl.sql.render_join)
        guru.dialect = None
        before.dialect = None
        yield ShopGuru(guru, conn, facts, path, before)
    finally:
        guru.close()
        before.close()
        conn.close()


def naive_counts(conn: Any) -> dict[str, int]:
    """The numbers the landing page's stories quote, counted directly in DuckDB.

    These are what an agent gets when it trusts names: the hub join's row count, a
    ``NOT deleted`` filter on a TRUE/NULL flag, and an exact-case enum filter.
    """

    def one(sql: str) -> int:
        return int(conn.execute(sql).fetchone()[0])

    return {
        "orders": one("SELECT COUNT(*) FROM shop.orders"),
        "orders_x_accounts_x_events": one(
            "SELECT COUNT(*) FROM shop.orders o "
            "JOIN shop.accounts a ON o.account_id = a.account_id "
            "JOIN shop.events e ON e.account_id = a.account_id"
        ),
        "not_deleted": one("SELECT COUNT(*) FROM shop.orders WHERE NOT deleted"),
        "live": one("SELECT COUNT(*) FROM shop.orders WHERE deleted IS NULL OR NOT deleted"),
        "status_paid_exact": one("SELECT COUNT(*) FROM shop.orders WHERE status = 'paid'"),
        "status_paid_any_case": one(
            "SELECT COUNT(*) FROM shop.orders WHERE lower(status) = 'paid'"
        ),
    }


def schema_dump(guru: sg.Guru) -> str:
    """The usual "paste the whole schema" prompt: every table and column, names and types."""
    out: list[str] = []
    for t in guru.store.iter_tables():
        cols = guru.store.columns(t.ref)
        body = ",\n".join(f"  {c.ref.column} {c.native_type}" for c in cols)
        out.append(f"CREATE TABLE {t.ref.canonical()} (\n{body}\n);")
    return "\n".join(out) + "\n"


def block(text: str, title: str) -> str:
    """One ``[title]`` table block cut out of a rendered view."""
    start = text.index(f"[{title}]")
    end = text.find("\n\n", start)
    return text[start : end if end != -1 else len(text)]
