"""The docs hook publishes the playground: its page assets, the built store and both wheels.

``build/playground`` is a build artefact (``scripts/build_playground_store.py`` plus the two
wheels), so a fake one stands in for it here; a missing piece must be a warning, because
``mkdocs build --strict`` then fails instead of shipping a playground that cannot start.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

import pytest
from mkdocs.config import load_config
from mkdocs.structure.files import Files

from .test_docs_generated import ROOT, _load

PLAYGROUND_PIECES = (
    "schemaguru-9.9.9-py3-none-any.whl",
    "factlang-0.1.4-py3-none-any.whl",
    "playground.db",
    "playground.json",
    "base_overlay.yaml",
)


def _publish(build: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    hooks = _load("mkdocs_hooks")
    monkeypatch.setattr(hooks, "PLAYGROUND_BUILD", build)
    config = load_config(str(ROOT / "mkdocs.yml"))
    config.plugins._current_plugin = None  # set by mkdocs while it runs hooks
    files = hooks.on_files(Files([]), config)
    return {f.src_uri: f for f in files}


def test_hook_publishes_playground(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in PLAYGROUND_PIECES:
        (tmp_path / name).write_bytes(b"x")
    files = _publish(tmp_path, monkeypatch)
    assert {
        "assets/playground/glue.py",
        "assets/playground/playground.js",
        "assets/playground/playground.css",
        "assets/playground/playground.db",
        "assets/playground/playground.json",
        "assets/playground/base_overlay.yaml",
        "assets/playground/schemaguru-9.9.9-py3-none-any.whl",
        "assets/playground/factlang-0.1.4-py3-none-any.whl",
        "assets/playground/wheels.json",
    } <= set(files)
    assert not any("__pycache__" in uri or uri.endswith(".pyc") for uri in files)
    assert json.loads(files["assets/playground/wheels.json"].content_string) == {
        "schemaguru": "schemaguru-9.9.9-py3-none-any.whl",
        "factlang": "factlang-0.1.4-py3-none-any.whl",
    }


def test_hook_warns_without_wheels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        files = _publish(tmp_path / "missing", monkeypatch)
    assert "playground cannot load" in caplog.text
    for piece in ("schemaguru", "factlang", "playground.db", "playground.json"):
        assert piece in caplog.text
    assert json.loads(files["assets/playground/wheels.json"].content_string) == {
        "schemaguru": None,
        "factlang": None,
    }


def test_hook_warns_for_one_missing_piece(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # negative control for the warning: a complete build is silent, one gap is not
    for name in PLAYGROUND_PIECES:
        (tmp_path / name).write_bytes(b"x")
    with caplog.at_level(logging.WARNING):
        _publish(tmp_path, monkeypatch)
    assert "playground" not in caplog.text
    (tmp_path / "playground.db").unlink()
    with caplog.at_level(logging.WARNING):
        _publish(tmp_path, monkeypatch)
    assert "playground.db" in caplog.text


def test_playground_page_shape() -> None:
    page = (ROOT / "docs" / "playground.md").read_text(encoding="utf-8")
    assert 'data-pyodide="https://cdn.jsdelivr.net/pyodide/v0.29.0/full/"' in page
    assert "```python" not in page
    for tab in ("ask", "path", "lineage", "privacy", "override"):
        assert f'id="sg-pg-tab-{tab}"' in page and f'id="sg-pg-panel-{tab}"' in page
    assert 'id="sg-pg-status"' in page and "aria-live" in page
    assert "<noscript>" in page and 'id="sg-pg-unsupported"' in page
    script = (ROOT / "site-playground" / "playground.js").read_text(encoding="utf-8")
    # results reach the DOM as text only; the only network origin is the Pyodide CDN
    assert "innerHTML" not in script
    assert not re.search(r"https?://", script), "the CDN comes from the page, nothing else"
