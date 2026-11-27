"""The docs theme keeps WCAG AA text contrast in both colour schemes and is wired into mkdocs."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CSS_PATH = ROOT / "docs" / "assets" / "schemaguru.css"


def _channel(c: int) -> float:
    s = c / 255
    return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4


def _luminance(hex_colour: str) -> float:
    h = hex_colour.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def contrast(a: str, b: str) -> float:
    """WCAG 2.x contrast ratio of two #rrggbb colours."""
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def scheme_tokens(css: str, scheme: str) -> dict[str, str]:
    """Every ``--name: #rrggbb`` declared in the ``[data-md-color-scheme=...]`` blocks."""
    tokens: dict[str, str] = {}
    pattern = r'\[data-md-color-scheme="' + re.escape(scheme) + r'"\]\s*\{([^}]*)\}'
    for block in re.findall(pattern, css):
        tokens.update(re.findall(r"(--[\w-]+):\s*(#[0-9a-fA-F]{6})\s*;", block))
    return tokens


@pytest.mark.parametrize("scheme", ["default", "slate"])
@pytest.mark.parametrize(
    "fg,bg",
    [
        ("--sg-fg", "--sg-bg"),
        ("--sg-fg-muted", "--sg-bg"),
        ("--sg-fg", "--sg-surface"),
        ("--sg-fg-muted", "--sg-surface"),
        ("--sg-accent-ink", "--sg-indigo"),
    ],
)
def test_text_tokens_meet_aa(scheme: str, fg: str, bg: str) -> None:
    tokens = scheme_tokens(CSS_PATH.read_text(encoding="utf-8"), scheme)
    assert contrast(tokens[fg], tokens[bg]) >= 4.5


def test_negative_control_contrast() -> None:
    assert contrast("#777777", "#888888") < 4.5
    assert contrast("#000000", "#ffffff") == pytest.approx(21)


def test_theme_wiring() -> None:
    class Loader(yaml.SafeLoader):
        pass

    Loader.add_multi_constructor("tag:yaml.org,2002:python/", lambda *_: None)
    Loader.add_multi_constructor("!", lambda *_: None)
    cfg = yaml.load((ROOT / "mkdocs.yml").read_text(encoding="utf-8"), Loader=Loader)
    assert cfg["theme"]["font"] is False
    assert cfg["theme"]["custom_dir"] == "docs/overrides"
    assert "assets/schemaguru.css" in cfg["extra_css"]
    assert "assets/extra.css" not in cfg["extra_css"]
    assert "assets/schemaguru.js" in cfg["extra_javascript"]
    assert not (ROOT / "docs" / "assets" / "extra.css").exists()
