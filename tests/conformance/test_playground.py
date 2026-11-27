"""The docs playground: its store builder and the Python glue the browser runs in Pyodide.

The store is built here from the shop fixture plus a small ``analytics`` layer of views (so
lineage has something to show); the glue runs against it in CPython exactly as Pyodide runs
it (``exec`` of ``site-playground/glue.py``). The clean-venv test mirrors docs.yml's smoke:
the glue needs only the two wheels and PyYAML.
"""

from __future__ import annotations

import json
import re
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


@pytest.fixture
def opened(playground: Path, glue: dict[str, Any]) -> dict[str, Any]:
    glue["sg_open"](str(playground / "playground.db"), (playground / "playground.json").read_text())
    return glue


def test_glue_errors_are_envelopes(opened: dict[str, Any]) -> None:
    for raw in (
        opened["sg_context"](""),
        opened["sg_context"]("x" * 501),
        opened["sg_path"]("shop.nope", "shop.orders"),
        opened["sg_lineage"]("shop.nope", "upstream", 2),
        opened["sg_context"]("q", "bogus"),
        opened["sg_mask"]("a", "raw"),
    ):
        out = json.loads(raw)
        assert set(out["error"]) >= {"code", "message", "generic"} and out["text"]
    codes = {
        json.loads(opened["sg_context"](""))["error"]["code"]: True,
        json.loads(opened["sg_context"]("q", "bogus"))["error"]["code"]: True,
        json.loads(opened["sg_mask"]("a", "raw"))["error"]["code"]: True,
    }
    assert codes == {"empty": True, "bad_profile": True, "bad_mode": True}
    assert json.loads(opened["sg_context"](""))["error"]["generic"] is True
    lib = json.loads(opened["sg_lineage"]("shop.nope", "upstream", 2))["error"]
    assert lib["generic"] == (not re.match(r"SG\d{3}$", lib["code"]))
    assert json.loads(opened["sg_mask"]("x" * 10_001))["error"]["code"] == "too_large"
    assert json.loads(opened["sg_override"]("x" * 20_001, "q"))["error"]["code"] == "too_large"
    bad = json.loads(opened["sg_override"]("tables: [unclosed", "orders"))
    assert bad["diagnostics"] and bad["diagnostics"][0]["code"].startswith("SG")


def test_override_wins_and_reset_restores(opened: dict[str, Any]) -> None:
    q = "active customers by segment"
    first = json.loads(opened["sg_context"](q))["fingerprint"]
    yaml_text = (
        "version: 1\nwarehouse: shop\ntables:\n  shop.customers:\n"
        "    description: Playground override wins.\n"
    )
    out = json.loads(opened["sg_override"](yaml_text, q))
    assert "Playground override wins." in out["after"]["text"]
    assert out["after"]["fingerprint"] != out["before"]["fingerprint"]
    assert json.loads(opened["sg_reset"](q))["fingerprint"] == first


def test_override_example_applies_cleanly(playground: Path, opened: dict[str, Any]) -> None:
    m = json.loads((playground / "playground.json").read_text())
    out = json.loads(opened["sg_override"](m["override_example"], "customers by segment"))
    assert out["diagnostics"] == []
    assert "Playground override wins." in out["after"]["text"]


def test_mask_modes(opened: dict[str, Any]) -> None:
    s = "ping jane.doe@example.org about status = 'vip'"
    m = json.loads(opened["sg_mask"](s, "masked"))
    n = json.loads(opened["sg_mask"](s, "none"))
    assert "jane.doe@example.org" not in m["text"] and "email" in m["categories"]
    # real behaviour: masked mode leaves a quoted literal in free text; none replaces it
    assert "'vip'" in m["text"] and "'vip'" not in n["text"] and "?" in n["text"]
    assert m["table_block"] != n["table_block"]
    # the config swap is restored
    assert json.loads(opened["sg_mask"](s, "masked"))["table_block"] == m["table_block"]


def test_version_mismatch_is_refused(playground: Path, glue: dict[str, Any]) -> None:
    manifest = json.loads((playground / "playground.json").read_text())
    manifest["schemaguru_version"] = "0.0.0"
    out = json.loads(glue["sg_open"](str(playground / "playground.db"), json.dumps(manifest)))
    assert out["error"]["code"] == "version_mismatch" and out["error"]["generic"] is True


def test_privacy_examples_are_synthetic(playground: Path, opened: dict[str, Any]) -> None:
    m = json.loads((playground / "playground.json").read_text())
    assert m["privacy_examples"] and m["precomputed"] == {}
    for ex in m["privacy_examples"]:
        assert not re.search(
            r"\b(ghp_|gho_|github_pat_|sk_live_|sk-|AKIA|AIza|xox[abp]-)", ex["text"]
        )
        out = json.loads(opened["sg_mask"](ex["text"], ex["mode"]))
        assert out["hits"] >= 1, ex
        if ex["mode"] == "none":
            assert "?" in out["text"]


def test_path_returns_no_invented_sql(opened: dict[str, Any]) -> None:
    out = json.loads(opened["sg_path"]("shop.payments", "shop.customers"))
    assert out["sql"] is None and "context view" in out["sql_note"]


def test_context_tokens_match_cpython(playground: Path, opened: dict[str, Any]) -> None:
    """The glue's token count is the library's, computed the same way in CPython."""
    from datetime import datetime

    m = json.loads((playground / "playground.json").read_text())
    q = "monthly revenue by customer segment"
    got = json.loads(opened["sg_context"](q))
    work = playground / "cpython-copy"
    work.mkdir(exist_ok=True)
    import shutil

    for name in (m["store"], m["overlay"]):
        shutil.copyfile(playground / name, work / name)
    now = datetime.fromisoformat(m["now"])
    with sg.Guru(
        store=str(work / m["store"]), connector=None, dialect="duckdb", name="shop", now=lambda: now
    ) as g:
        g.load_overlays(work / m["overlay"])
        v = g.context(q, profile="agent", budget_tokens=4000)
    assert (got["tokens"], got["fingerprint"], got["text"]) == (v.tokens, v.fingerprint, v.text)


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
