"""The built landing page in a real browser: narrow, dark, reduced motion, keyboard tabs.

Needs a built site (``mkdocs build``) and Playwright with Chromium; skipped otherwise. The site
is served from ``site/`` on a random local port; nothing leaves the machine.
"""

from __future__ import annotations

import functools
import http.server
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.integration

sync_api = pytest.importorskip("playwright.sync_api")

SITE = Path(__file__).resolve().parents[2] / "site"


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass


@pytest.fixture(scope="module")
def base() -> Iterator[str]:
    if not (SITE / "index.html").is_file():
        pytest.skip("site/ is not built; run mkdocs build")
    handler = functools.partial(_Quiet, directory=str(SITE))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    with sync_api.sync_playwright() as pw:
        try:
            b = pw.chromium.launch()
        except Exception as e:  # no browser binary installed
            pytest.skip(f"Chromium is not available: {e}")
        yield b
        b.close()


@pytest.fixture
def page(browser: Any) -> Iterator[Any]:
    context = browser.new_context()
    # the site's own files only: anything else would be an external request
    context.route("**/*", lambda r: r.continue_() if "127.0.0.1" in r.request.url else r.abort())
    p = context.new_page()
    yield p
    context.close()


def test_landing_narrow_dark_reduced_motion(page: Any, base: str) -> None:
    page.set_viewport_size({"width": 360, "height": 780})
    page.emulate_media(color_scheme="dark", reduced_motion="reduce")
    page.goto(base)
    assert page.evaluate("document.documentElement.scrollWidth") <= 360
    assert page.locator(".sg-reveal").first.evaluate("e => getComputedStyle(e).opacity") == "1"
    assert page.locator("#sg-before-after mark[data-trap]").count() >= 2
    assert page.locator("#sg-warehouses .sg-badge").count() == 11
    fits = "e => e.scrollWidth <= e.clientWidth"
    for chip in page.locator(".sg-chip").all():
        assert chip.evaluate(fits), "the install chip clips its text"
    assert page.locator(".sg-integrations [role=tablist]").evaluate(fits), "tabs hidden off-screen"


@pytest.mark.parametrize("width", [375, 1280, 1920])
def test_sections_share_one_container(page: Any, base: str, width: int) -> None:
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(base)
    boxes = page.eval_on_selector_all(
        "section.sg-section",
        "els => els.map(e => { const b = e.getBoundingClientRect();"
        " return [Math.round(b.left), Math.round(b.width)]; })",
    )
    assert len(boxes) == 8 and len(set(map(tuple, boxes))) == 1, boxes


def test_sections_render_in_order(page: Any, base: str) -> None:
    page.goto(base)
    ids = page.eval_on_selector_all("section[id^='sg-']", "els => els.map(e => e.id)")
    assert ids == [
        "sg-hero",
        "sg-before-after",
        "sg-benefits",
        "sg-how",
        "sg-warehouses",
        "sg-trust",
        "sg-cta",
    ]
    assert page.locator(".sg-integrations").count() == 1


def test_tabs_keyboard(page: Any, base: str) -> None:
    page.goto(base)
    tabs = page.locator(".sg-integrations [role=tab]")
    assert tabs.count() == 5
    assert tabs.nth(0).get_attribute("aria-selected") == "true"
    assert page.locator("#sg-panel-mcp").is_hidden()
    tabs.nth(0).focus()
    page.keyboard.press("ArrowRight")
    assert tabs.nth(1).get_attribute("aria-selected") == "true"
    assert tabs.nth(0).get_attribute("aria-selected") == "false"
    assert page.locator("#sg-panel-mcp").is_visible()
    assert page.locator("#sg-panel-python").is_hidden()
    assert page.evaluate("document.activeElement.id") == "sg-tab-mcp"
    page.keyboard.press("End")
    assert tabs.nth(4).get_attribute("aria-selected") == "true"


_LUMINANCE = """c => { const v = c.match(/[\\d.]+/g).map(Number);
  const ch = x => { x /= 255; return x <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4; };
  return [0.2126 * ch(v[0]) + 0.7152 * ch(v[1]) + 0.0722 * ch(v[2]), v.length > 3 ? v[3] : 1]; }"""

_FOOTER_COLOURS = """e => { let b = e, bg;
  while ((bg = getComputedStyle(b).backgroundColor) === "rgba(0, 0, 0, 0)") b = b.parentElement;
  return [getComputedStyle(e).color, bg]; }"""


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_footer_text_contrast(page: Any, base: str, scheme: str) -> None:
    page.emulate_media(color_scheme=scheme)
    page.goto(base)
    for selector in (".md-copyright", ".md-footer__link"):
        fg, bg = page.locator(selector).first.evaluate(_FOOTER_COLOURS)
        (lf, af), (lb, ab) = page.evaluate(_LUMINANCE, fg), page.evaluate(_LUMINANCE, bg)
        assert af == 1 and ab == 1, (selector, fg, bg)  # opaque, so the ratio is exact
        hi, lo = max(lf, lb), min(lf, lb)
        assert (hi + 0.05) / (lo + 0.05) >= 4.5, (selector, fg, bg)


@pytest.mark.parametrize("width", [375, 1280])
def test_after_pane_opens_on_a_whole_line(page: Any, base: str, width: int) -> None:
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(base)
    page.wait_for_load_state("load")
    code = page.locator("#sg-before-after .sg-pane--after code")
    assert code.evaluate("c => c.scrollTop") > 0  # opened on the traps, not on the legend
    first = code.evaluate(
        """c => { const box = c.getBoundingClientRect();
          const line = [...c.querySelectorAll(".sg-line")]
            .find(l => l.textContent.trim() && l.getBoundingClientRect().bottom > box.top + 1);
          const r = line.getBoundingClientRect();
          return [r.top - box.top, line.textContent]; }"""
    )
    assert first[0] >= 4, first  # the first visible text line is whole, below the padding
    assert first[1].startswith("["), first  # the table header line opens the view
