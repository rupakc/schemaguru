"""Container-backed readers: seed a small shop schema per engine and run the same assertions as
the recorded-fixture conformance test (``tests/conformance``), plus paging and incremental reads.

Needs ``SCHEMAGURU_TEST_<ENGINE>_DSN`` (see ``tests/integration/conftest.py`` for the policy).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from schemaguru.catalog import reader_for
from schemaguru.model.structure import Schema
from tests.conformance.replay import FIXTURES, read_all
from tests.integration.catalog.engines import ENGINES, Engine
from tests.integration.conftest import engine_dsn, require_driver

pytestmark = pytest.mark.integration

SEEDED = {"sg_shop", "sg_ops"}
VOLATILE = ("created_at", "last_altered", "bytes")


def restrict(out: dict[str, Any], *, mask_rows: bool) -> dict[str, Any]:
    """Keep only the seeded schemas and drop values that change run to run."""
    tables = []
    for t in out["tables"]:
        if t["ref"]["schema"] in SEEDED:
            t = {k: v for k, v in t.items() if k not in VOLATILE}
            if mask_rows:
                t.pop("row_count")
            tables.append(t)
    return {
        "schemas": [s for s in out["schemas"] if s["name"] in SEEDED],
        "tables": tables,
        "columns": [c for c in out["columns"] if c["ref"]["schema"] in SEEDED],
        "constraints": [c for c in out["constraints"] if c["table"]["schema"] in SEEDED],
    }


@pytest.fixture(scope="module", params=sorted(ENGINES))
def live(request: pytest.FixtureRequest) -> Iterator[tuple[Engine, Any]]:
    engine = ENGINES[request.param]
    dsn = engine_dsn(engine.name)
    require_driver(engine.name, "pymssql" if dsn.startswith("pymssql://") else engine.driver)
    conn = engine.connect(dsn)
    engine.seed(conn)
    yield engine, conn
    conn.close()


def test_reader_matches_recorded_expectation(live: tuple[Engine, Any]) -> None:
    engine, conn = live
    reader = reader_for(engine.name, engine.executor(conn))
    got = read_all(reader, only_catalogs=engine.only_catalogs)
    expected = json.loads((FIXTURES / engine.name / "expected.json").read_text(encoding="utf-8"))
    mask_rows = engine.name == "mysql"  # InnoDB TABLE_ROWS is an estimate
    assert restrict(got, mask_rows=mask_rows) == restrict(expected, mask_rows=mask_rows)


def test_paging_returns_every_table_once(live: tuple[Engine, Any]) -> None:
    engine, conn = live
    cat = engine.only_catalogs[0] if engine.only_catalogs else None
    schema = Schema(cat, "sg_shop")
    full = reader_for(engine.name, engine.executor(conn)).list_tables(
        schema, page_token=None, changed_since=None
    )
    small = reader_for(engine.name, engine.executor(conn))
    small.page_size = 2  # type: ignore[attr-defined]
    names: list[str] = []
    token = None
    pages = 0
    while True:
        page = small.list_tables(schema, page_token=token, changed_since=None)
        names += [t.ref.table for t in page.items]
        pages += 1
        token = page.next_token
        if token is None:
            break
    assert names == [t.ref.table for t in full.items]
    assert len(set(names)) == len(names) == 4 and pages == 2


def test_changed_since_in_the_future_lists_nothing(live: tuple[Engine, Any]) -> None:
    engine, conn = live
    reader = reader_for(engine.name, engine.executor(conn))
    if not reader.capabilities().incremental:
        pytest.skip(f"{engine.name} has no incremental listing")
    schema = Schema(None, "sg_shop")
    future = datetime.now(timezone.utc) + timedelta(days=1)
    # a table whose alteration time the engine does not report is never silently dropped
    later = reader.list_tables(schema, page_token=None, changed_since=future).items
    assert all(t.last_altered is None for t in later)
    past = datetime.now(timezone.utc) - timedelta(days=1)
    assert len(reader.list_tables(schema, page_token=None, changed_since=past).items) >= 1
