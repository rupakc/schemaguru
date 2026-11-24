"""Generated documentation is current: the committed files equal a fresh generation.

``scripts/gen_docs.py`` writes the rendered shop outputs quoted by the docs, the CLI reference
(from argparse) and the diagnostics table (from ``CODES``); ``scripts/build_demo_export.py``
writes the site demo's knowledge export. Text the docs show as SchemaGuru output is therefore
never hand-written, and it cannot drift from the code. Regenerate with those two scripts.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


def _load(name: str) -> ModuleType:
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def generated() -> dict[str, str]:
    pytest.importorskip("duckdb")
    return dict(_load("gen_docs").generate())


@pytest.fixture(scope="module")
def export_text() -> str:
    pytest.importorskip("duckdb")
    return str(_load("build_demo_export").render())


def test_generated_docs_are_current(generated: dict[str, str]) -> None:
    stale = [
        rel
        for rel, text in sorted(generated.items())
        if not (ROOT / "docs" / rel).exists()
        or (ROOT / "docs" / rel).read_text(encoding="utf-8") != text
    ]
    assert not stale, f"stale generated docs (run python scripts/gen_docs.py): {stale}"


def test_no_orphan_generated_files(generated: dict[str, str]) -> None:
    on_disk = {
        f"generated/{p.name}" for p in (ROOT / "docs" / "generated").iterdir() if p.is_file()
    }
    assert on_disk == {k for k in generated if k.startswith("generated/")}


def _without_layout(data: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in data.items() if k != "layout"}


def test_demo_export_is_current(export_text: str) -> None:
    fresh = json.loads(export_text)
    committed = json.loads((ROOT / "site-demo" / "shop_export.json").read_text(encoding="utf-8"))
    assert _without_layout(committed) == _without_layout(fresh), (
        "stale site-demo/shop_export.json (run python scripts/build_demo_export.py)"
    )
    # float layout: equal up to rounding noise across platforms
    assert committed["layout"].keys() == fresh["layout"].keys()
    for name, (x, y) in fresh["layout"].items():
        cx, cy = committed["layout"][name]
        assert abs(cx - x) < 1e-3 and abs(cy - y) < 1e-3, name


EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
HEX32 = re.compile(r"\b[0-9a-f]{32}\b")


def test_demo_export_holds_no_raw_identifying_values(export_text: str) -> None:
    # the shop's customers.email holds user<n>@example.com; anon_enum_t.kind 32-hex tokens
    assert not EMAIL.search(export_text)
    assert not HEX32.search(export_text)


def test_demo_export_tells_the_three_stories(export_text: str) -> None:
    data = json.loads(export_text)
    assert data["paths"]["shop.orders|shop.events"]["refusal"] == "HUB_PIVOT"
    assert [h["table"] for h in data["hubs"]] == ["shop.accounts"]
    orders = next(t for t in data["tables"] if t["name"] == "shop.orders")["block"]
    assert "case drift" in orders and "live rows:" in orders
    counts = data["naive_counts"]
    assert counts["not_deleted"] == 0 and counts["orders_x_accounts_x_events"] > counts["orders"]


def test_negative_control_raw_value_detectors() -> None:
    assert EMAIL.search('{"v": "user7@example.com"}')
    assert HEX32.search("0123456789abcdef0123456789abcdef@anon.example")
