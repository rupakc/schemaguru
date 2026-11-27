"""Build the docs playground's store: the shop fixture plus a small ``analytics`` layer of views.

Usage: ``python3 scripts/build_playground_store.py [--out build/playground]``

The browser cannot crawl or parse SQL (lineage parsing runs in a worker subprocess and needs the
``lineage`` extra), so everything is computed here in CPython: the shop is crawled with its
views, view lineage is parsed, every table is measured and the spec overlay is loaded. The
browser then serves the store offline (``Guru(store=..., connector=None)``).

Writes into ``--out``: ``playground.db`` (one file: journal_mode DELETE, VACUUMed),
``base_overlay.yaml`` (a copy of the overlay) and ``playground.json`` (the manifest). ``build/``
is gitignored: docs.yml builds the store from the same commit; it is never committed.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _shop_docs import NOW, OVERLAY, QUESTIONS, ROOT, shop_guru

import schemaguru as sg
from schemaguru.lineage.parse import lineage_available
from schemaguru.store.sqlite import SEARCH_BACKEND_KEY

FORMAT = "schemaguru-playground/1"
STORE = "playground.db"
MANIFEST = "playground.json"
BASE_OVERLAY = "base_overlay.yaml"

# the shop has no amount on orders; payments.amount carries it
ANALYTICS_VIEWS: tuple[str, ...] = (
    "CREATE SCHEMA IF NOT EXISTS analytics",
    "CREATE VIEW analytics.paid_orders AS"
    " SELECT order_id, customer_id, created_at FROM shop.orders"
    " WHERE lower(status) = 'paid' AND (deleted IS NULL OR NOT deleted)",
    "CREATE VIEW analytics.paid_revenue AS"
    " SELECT p.order_id, po.customer_id, p.amount"
    " FROM shop.payments p JOIN analytics.paid_orders po ON p.order_id = po.order_id",
    "CREATE VIEW analytics.revenue_by_segment AS"
    " SELECT c.segment, SUM(r.amount) AS revenue, COUNT(DISTINCT r.order_id) AS orders"
    " FROM analytics.paid_revenue r JOIN shop.customers c ON r.customer_id = c.customer_id"
    " GROUP BY c.segment",
)
LINEAGE_SUBJECTS = (
    "analytics.paid_orders",
    "analytics.paid_revenue",
    "analytics.revenue_by_segment",
    "analytics.revenue_by_segment.revenue",
    "analytics.revenue_by_segment.segment",
)
# synthetic on purpose: nothing here resembles a real provider's key format
PRIVACY_EXAMPLES: tuple[dict[str, str], ...] = (
    {"label": "E-mail", "mode": "masked", "text": "Ping jane.doe@example.org about the refund."},
    {"label": "Phone", "mode": "masked", "text": "Call +44 20 7946 0958 before noon."},
    {"label": "IBAN", "mode": "masked", "text": "Refund to GB82 WEST 1234 5698 7654 32 today."},
    {
        "label": "Card number",
        "mode": "masked",
        "text": "Test card 4111 1111 1111 1111 was declined.",
    },
    {
        "label": "Key and value",
        "mode": "masked",
        "text": "connect with password=example-not-a-secret",
    },
    {
        "label": "Quoted literal",
        "mode": "none",
        "text": "Mail jane.doe@example.org where segment = 'vip'",
    },
)
OVERRIDE_EXAMPLE = """\
version: 1
warehouse: shop
tables:
  shop.customers:
    description: Playground override wins.
    pitfalls:
      - rule: segment is free text entered by sales; do not group on it without lower()
        fix: group on lower(segment)
lineage:
  - table: analytics.paid_revenue
    ignore: true
"""
EXTRA_QUESTIONS = ("revenue by customer segment from the analytics views",)


class LineageUnavailable(RuntimeError):
    """The ``lineage`` extra (sqlglot) is not installed: the views' lineage cannot be parsed."""


def _single_file(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("VACUUM")
    finally:
        conn.close()


def build(out: Path) -> dict[str, Any]:
    """Build the store, the overlay copy and the manifest in ``out``; returns the manifest."""
    if not lineage_available():
        raise LineageUnavailable(
            "the playground needs view lineage: install the lineage extra (pip install "
            "'schemaguru[lineage]')"
        )
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        with shop_guru(Path(tmp), views=ANALYTICS_VIEWS) as shop:
            store = shop.guru.store
            tables = sorted(t.ref.canonical() for t in store.iter_tables())
            backend = (store.crawl_state_get(SEARCH_BACKEND_KEY) or {}).get("backend")
        # closed: the WAL is checkpointed into the file before it is copied
        shutil.copyfile(Path(tmp) / "shop.db", out / STORE)
    _single_file(out / STORE)
    shutil.copyfile(OVERLAY, out / BASE_OVERLAY)
    manifest: dict[str, Any] = {
        "format": FORMAT,
        "schemaguru_version": sg.__version__,
        "now": NOW.isoformat(),
        "store": STORE,
        "overlay": BASE_OVERLAY,
        "tables": tables,
        "lineage_subjects": list(LINEAGE_SUBJECTS),
        "questions": [*QUESTIONS, *EXTRA_QUESTIONS],
        "search_backend_at_build": backend,
        "privacy_examples": [
            {"label": e["label"], "text": e["text"], "mode": e["mode"]} for e in PRIVACY_EXAMPLES
        ],
        "override_example": OVERRIDE_EXAMPLE,
        "precomputed": {},
    }
    (out / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--out", type=Path, default=ROOT / "build" / "playground")
    args = parser.parse_args(argv)
    try:
        manifest = build(args.out)
    except LineageUnavailable as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"wrote {args.out / STORE} ({len(manifest['tables'])} tables)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
