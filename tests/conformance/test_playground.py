"""The docs playground: its store builder and the Python glue the browser runs in Pyodide.

The store is built here from the shop fixture plus a small ``analytics`` layer of views (so
lineage has something to show); the glue runs against it in CPython exactly as Pyodide runs
it (``exec`` of ``site-playground/glue.py``). The clean-venv test mirrors docs.yml's smoke:
the glue needs only the two wheels and PyYAML.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import schemaguru as sg

ROOT = Path(__file__).resolve().parents[2]
GLUE = ROOT / "site-playground" / "glue.py"
PLAYGROUND_BUILD = ROOT / "build" / "playground"
sys.path.insert(0, str(ROOT / "scripts"))

from _shop_docs import OVERLAY  # noqa: E402


@pytest.fixture(scope="module")
def playground(tmp_path_factory: pytest.TempPathFactory) -> Path:
    pytest.importorskip("duckdb")
    pytest.importorskip("sqlglot")
    from build_playground_store import build

    out = tmp_path_factory.mktemp("playground")
    build(out)
    return out


@pytest.fixture
def glue() -> dict[str, Any]:
    ns: dict[str, Any] = {"__name__": "glue"}
    exec(compile(GLUE.read_text(encoding="utf-8"), str(GLUE), "exec", dont_inherit=True), ns)
    return ns


def test_store_has_shop_and_analytics(playground: Path) -> None:
    m = json.loads((playground / "playground.json").read_text())
    assert m["format"] == "schemaguru-playground/1" and m["schemaguru_version"] == sg.__version__
    assert {
        "shop.orders",
        "shop.customers",
        "shop.payments",
        "shop.events",
        "analytics.paid_orders",
        "analytics.paid_revenue",
        "analytics.revenue_by_segment",
    } <= set(m["tables"])
    assert m["tables"] == sorted(m["tables"])
    assert m["search_backend_at_build"] in ("fts5", "like")
    assert (playground / "playground.db").stat().st_size < 20 * 2**20
    assert (playground / "base_overlay.yaml").read_bytes() == OVERLAY.read_bytes()


def test_store_is_a_single_file(playground: Path) -> None:
    """Pyodide gets one file: no WAL journal, nothing beside the store."""
    import sqlite3

    conn = sqlite3.connect(str(playground / "playground.db"))
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    finally:
        conn.close()
    assert not (playground / "playground.db-wal").exists()


def test_lineage_is_precomputed(playground: Path) -> None:
    with sg.Guru(store=str(playground / "playground.db"), connector=None) as g:
        up = {n.ref for n in g.lineage("analytics.revenue_by_segment", depth=3).nodes}
        down = {n.ref for n in g.lineage("shop.orders", "downstream", 1).nodes}
    assert {"shop.payments", "shop.orders", "shop.customers"} <= up
    assert "analytics.paid_orders" in down


def test_glue_core_in_cpython(playground: Path, glue: dict[str, Any]) -> None:
    opened = json.loads(
        glue["sg_open"](
            str(playground / "playground.db"), (playground / "playground.json").read_text()
        )
    )
    assert opened["ok"] is True
    view = json.loads(glue["sg_context"]("monthly revenue by customer segment"))
    assert view["tokens"] > 0 and "shop.orders" in view["tables"]
    assert json.loads(glue["sg_path"]("shop.orders", "shop.events"))["refusal"] == "HUB_PIVOT"
    ok = json.loads(glue["sg_path"]("shop.payments", "shop.customers"))
    # the find_join_path tool lists join keys; it renders no SQL (only context views do)
    assert ok["refusal"] is None and ok["paths"]
    assert "payments.order_id = orders.order_id" in ok["text"]
    assert (
        json.loads(glue["sg_path"]("shop.broken_child", "shop.ref_parent"))["refusal"] is not None
    )
    assert (
        "shop.payments"
        in json.loads(glue["sg_lineage"]("analytics.revenue_by_segment", "upstream", 3))["text"]
    )
    assert "SQL: FROM " in json.loads(glue["sg_context"]("payments per customer"))["text"]
    masked = json.loads(glue["sg_mask"]("write to jane.doe@example.org", "masked"))
    assert "jane.doe@example.org" not in masked["text"] and masked["hits"] >= 1
    assert glue["smoke"](str(playground)).startswith("ok ")


def test_glue_errors_use_the_tool_envelope(playground: Path, glue: dict[str, Any]) -> None:
    glue["sg_open"](str(playground / "playground.db"), (playground / "playground.json").read_text())
    bad = json.loads(glue["sg_lineage"]("analytics.revenue_by_segment", "sideways"))
    assert set(bad) == {"error", "text"} and bad["text"].startswith(bad["error"]["code"] + " ")
    unopened: dict[str, Any] = {"__name__": "glue"}
    exec(compile(GLUE.read_text(encoding="utf-8"), str(GLUE), "exec"), unopened)
    err = json.loads(unopened["sg_context"]("anything"))["error"]
    assert err["code"] and err["message"]


@pytest.mark.integration
def test_glue_in_clean_venv(tmp_path: Path) -> None:
    """The glue imports only the two wheels + PyYAML (no sqlglot, no duckdb): docs.yml's smoke."""
    wheels = sorted(PLAYGROUND_BUILD.glob("*.whl"))
    names = {w.name.split("-")[0] for w in wheels}
    if not {"schemaguru", "factlang"} <= names or not (PLAYGROUND_BUILD / "playground.db").exists():
        pytest.skip("run PREP first: build/playground needs both wheels and the store")
    venv = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    py = venv / "bin" / "python"
    subprocess.run(
        [str(py), "-m", "pip", "install", "-q", *map(str, wheels)], check=True, cwd=tmp_path
    )
    out = subprocess.run(
        [str(py), "-I", str(GLUE), str(PLAYGROUND_BUILD)],
        check=True,
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert out.stdout.startswith("ok "), out.stdout + out.stderr
