"""Privacy of view definitions (0.2.0 spec 4.3): part 1, the crawl; part 2, every surface a
lineage fact can reach (render, tools, HTTP, CLI, diagnostics, logs)."""

from __future__ import annotations

import importlib.util
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


# ----- part 2: crawl -> store -> render -> tool -> HTTP / CLI ----------------------------------


def test_literals_absent_from_every_surface(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    from schemaguru.cli import main
    from schemaguru.guru import Guru
    from schemaguru.render.profiles import profile_names

    caplog.set_level(logging.DEBUG)
    reader, literals = privacy_warehouse(tmp_path)
    db = tmp_path / "guru.db"
    surfaces: dict[str, str] = {}
    with Guru(reader=reader, store=str(db), dialect="duckdb") as guru:
        guru.crawl()
        assert guru.lineage("shop.v_vip").edges  # the control: lineage was parsed and stored
        for profile in profile_names():
            surfaces[f"context:{profile}"] = guru.context("vip customers", profile=profile).text
        calls = {
            "describe_table": {"table": "shop.v_vip"},
            "lineage": {"ref": "shop.v_vip"},
            "lineage:column": {"ref": "shop.v_vip.email"},
            "lineage:down": {"ref": "shop.customers", "direction": "downstream"},
            "schema_context": {"question": "vip customers"},
        }
        for key, args in calls.items():
            out = guru.handle_tool_call(key.split(":")[0], args)
            assert "error" not in out, (key, out)
            surfaces[f"tool:{key}"] = json.dumps(out, ensure_ascii=False)
        assert "shop.customers" in surfaces["tool:lineage"]
        if importlib.util.find_spec("starlette") and importlib.util.find_spec("httpx"):
            from starlette.testclient import TestClient

            from schemaguru.server import BearerTokens, create_app

            token = "reader-token-0123456789"
            app = create_app(guru, authenticator=BearerTokens({token: "reader"}))
            client = TestClient(app, raise_server_exceptions=False)
            headers = {"Authorization": f"Bearer {token}"}
            for path in ("/lineage?ref=shop.v_vip", "/tables/shop.v_vip",
                         "/facts?subject=shop.v_vip"):  # fmt: skip
                r = client.get(path, headers=headers)
                assert r.status_code == 200, (path, r.text)
                surfaces[f"http:{path}"] = r.text
        surfaces["diagnostics"] = " ".join(
            f"{d.message} {d.hint or ''} {d.location or ''}" for d in guru.diagnostics
        )
    capsys.readouterr()
    code = main(["lineage", "shop.v_vip", "--store", str(db), "--format", "json"])
    cli = capsys.readouterr()
    assert code == 0, cli.err
    assert "shop.customers" in cli.out
    surfaces["cli"] = cli.out + cli.err
    surfaces["logs"] = caplog.text
    for name, text in surfaces.items():
        for literal in literals:
            assert literal not in text, (name, literal)
