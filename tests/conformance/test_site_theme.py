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


_INKS = ["--sg-teal-ink", "--sg-amber-ink", "--sg-sky-ink", "--sg-rose-ink"]


@pytest.mark.parametrize("scheme", ["default", "slate"])
@pytest.mark.parametrize("bg", ["--sg-bg", "--sg-surface", "--sg-code-bg"])
@pytest.mark.parametrize("ink", _INKS)
def test_accent_ink_text_meets_aa(scheme: str, bg: str, ink: str) -> None:
    tokens = scheme_tokens(CSS_PATH.read_text(encoding="utf-8"), scheme)
    assert contrast(tokens[ink], tokens[bg]) >= 4.5


@pytest.mark.parametrize("scheme", ["default", "slate"])
@pytest.mark.parametrize("bg", ["--sg-bg", "--sg-surface", "--sg-code-bg"])
def test_focus_outline_meets_non_text_contrast(scheme: str, bg: str) -> None:
    tokens = scheme_tokens(CSS_PATH.read_text(encoding="utf-8"), scheme)
    assert contrast(tokens["--sg-focus"], tokens[bg]) >= 3.0


@pytest.mark.parametrize("scheme", ["default", "slate"])
@pytest.mark.parametrize("fg", ["--sg-header-fg", "--sg-header-fg-muted"])
def test_header_text_meets_aa(scheme: str, fg: str) -> None:
    tokens = scheme_tokens(CSS_PATH.read_text(encoding="utf-8"), scheme)
    assert contrast(tokens[fg], tokens["--sg-header-bg"]) >= 4.5


def test_header_maps_material_text_to_header_tokens() -> None:
    css = CSS_PATH.read_text(encoding="utf-8")
    assert "--md-primary-bg-color: var(--sg-header-fg)" in css
    assert "--md-primary-bg-color--light: var(--sg-header-fg-muted)" in css


def test_reveal_hidden_state_is_gated_on_js() -> None:
    css = CSS_PATH.read_text(encoding="utf-8")
    for selector, body in re.findall(r"([^{}]+)\{([^}]*)\}", css):
        if ".sg-reveal" in selector and re.search(r"opacity:\s*0\b", body):
            assert ".sg-js" in selector and ":not(.is-visible)" in selector, selector


def test_js_sets_sg_js_class() -> None:
    js = (ROOT / "docs" / "assets" / "schemaguru.js").read_text(encoding="utf-8")
    assert 'classList.add("sg-js")' in js


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


def test_footer_maps_material_text_and_band_to_tokens() -> None:
    """Material's footer band and copyright default to translucent black/white (about 2:1)."""
    css = CSS_PATH.read_text(encoding="utf-8")
    assert "--md-footer-fg-color--lighter: var(--sg-fg-muted)" in css
    assert "--md-footer-bg-color--dark: var(--sg-bg)" in css


@pytest.mark.parametrize("scheme", ["default", "slate"])
def test_footer_text_meets_aa(scheme: str) -> None:
    tokens = scheme_tokens(CSS_PATH.read_text(encoding="utf-8"), scheme)
    assert contrast(tokens["--sg-fg-muted"], tokens["--sg-bg"]) >= 4.5
