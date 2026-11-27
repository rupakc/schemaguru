"""Shared links get a preview card, and the home tab title is not "SchemaGuru - SchemaGuru"."""

from __future__ import annotations

import re
import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OVERRIDES = ROOT / "docs" / "overrides"
CARD = ROOT / "docs" / "assets" / "social-card.png"


def _png_size(path: Path) -> tuple[int, int]:
    head = path.read_bytes()[:24]
    assert head[:8] == b"\x89PNG\r\n\x1a\n", f"{path} is not a PNG"
    width, height = struct.unpack(">II", head[16:24])
    return width, height


def test_social_card_is_a_1200x630_png() -> None:
    assert _png_size(CARD) == (1200, 630)


def test_every_page_declares_the_card() -> None:
    main = (OVERRIDES / "main.html").read_text(encoding="utf-8")
    assert '{% extends "base.html" %}' in main
    assert "{{ config.site_url }}assets/social-card.png" in main
    assert '<meta name="twitter:card" content="summary_large_image">' in main
    for name in ("home.html", "redirect.html"):  # both inherit it through super()
        tpl = (OVERRIDES / name).read_text(encoding="utf-8")
        assert '{% extends "main.html" %}' in tpl and "{{ super() }}" in tpl, name


def test_home_title_is_not_the_site_name_twice() -> None:
    home = (OVERRIDES / "home.html").read_text(encoding="utf-8")
    m = re.search(r"{% block htmltitle %}\s*<title>([^<]+)</title>", home)
    assert m, "home.html must override htmltitle"
    assert m.group(1) != "SchemaGuru - SchemaGuru" and m.group(1).startswith("SchemaGuru")


def test_negative_control_png_size_reader(tmp_path: Path) -> None:
    bad = tmp_path / "x.png"
    bad.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 8 + struct.pack(">II", 1200, 600))
    assert _png_size(bad) != (1200, 630)
