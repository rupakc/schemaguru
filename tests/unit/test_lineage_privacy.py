"""Privacy of view definitions (0.2.0 spec 4.3), part 1: the crawl."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from schemaguru.catalog.crawl import crawl
from schemaguru.lineage import lineage_available
from schemaguru.model.refs import ObjectRef
from schemaguru.store.export import export_json
from schemaguru.store.sqlite import SQLiteStore
from tests.unit.lineage_helpers import privacy_warehouse

pytestmark = pytest.mark.skipif(not lineage_available(), reason="needs the lineage extra")


def _encodings(text: str) -> list[bytes]:
    return [text.encode(e) for e in ("utf-8", "utf-16-le", "utf-16-be")]


def test_view_literals_never_stored_or_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    reader, literals = privacy_warehouse(tmp_path)
    db = tmp_path / "store.db"
    store = SQLiteStore(db)
    report = crawl(reader, store)
    assert report.views_parsed == 1
    view = ObjectRef(None, "shop", "v_vip")
    table = next(f for f in store.facts_for([view], ["LINEAGE"]))
    assert table.value["upstream"] == ["shop.customers"]
    assert table.evidence is not None and table.evidence.sql is not None
    assert "email = ?" in table.evidence.sql
    exported = tmp_path / "export.json"
    export_json(store, exported)
    store.close()

    blobs = [exported.read_bytes()]
    for suffix in ("", "-wal", "-shm"):
        path = Path(f"{db}{suffix}")
        if path.exists():
            blobs.append(path.read_bytes())
    diagnostics = " ".join(
        f"{d.message} {d.hint or ''} {d.location or ''}"
        for d in [*report.errors, *report.lineage_diagnostics]
    )
    for literal in literals:
        for enc in _encodings(literal):
            assert not any(enc in b for b in blobs), literal
        assert literal not in caplog.text, literal
        assert literal not in diagnostics, literal
