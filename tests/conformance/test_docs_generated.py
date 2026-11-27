"""Generated documentation is current: the committed files equal a fresh generation.

``scripts/gen_docs.py`` writes the rendered shop outputs quoted by the docs, the CLI reference
(from argparse) and the diagnostics table (from ``CODES``); ``scripts/build_playground_store.py``
builds the playground's store in CI only. Text the docs show as SchemaGuru output is therefore
never hand-written, and it cannot drift from the code. Regenerate with ``gen_docs.py``.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

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


EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
HEX32 = re.compile(r"\b[0-9a-f]{32}\b")


def test_negative_control_raw_value_detectors() -> None:
    assert EMAIL.search('{"v": "user7@example.com"}')
    assert HEX32.search("0123456789abcdef0123456789abcdef@anon.example")
