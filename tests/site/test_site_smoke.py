"""The built landing page in a real browser: narrow, dark, reduced motion, keyboard tabs.

Needs a built site (``mkdocs build``) and Playwright with a browser; skipped otherwise. Runs in
Chromium; ``SG_BROWSER=firefox`` or ``SG_BROWSER=webkit`` (Safari's engine) picks another. The site
is served from ``site/`` on a random local port; nothing leaves the machine.
"""

from __future__ import annotations

import functools
import http.server
import os
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


ENGINE = os.environ.get("SG_BROWSER", "chromium")  # chromium | firefox | webkit (Safari's engine)


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    if ENGINE not in ("chromium", "firefox", "webkit"):
        pytest.fail(f"SG_BROWSER={ENGINE!r}: use chromium, firefox or webkit")
    with sync_api.sync_playwright() as pw:
        try:
            b = getattr(pw, ENGINE).launch()
        except Exception as e:  # no browser binary installed
            pytest.skip(f"{ENGINE} is not available: {e}")
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


# ----- the playground: the real wheels in Pyodide ------------------------------------------

PYODIDE_CDN = "https://cdn.jsdelivr.net/pyodide/v0.29.0/full/"


@pytest.fixture
def pg_page(browser: Any) -> Iterator[Any]:
    """The site plus the Pyodide CDN, nothing else (needs network for the CDN)."""
    context = browser.new_context()
    allowed = ("http://127.0.0.1", PYODIDE_CDN)
    context.route(
        "**/*", lambda r: r.continue_() if r.request.url.startswith(allowed) else r.abort()
    )
    p = context.new_page()
    p.set_default_timeout(30_000)
    yield p
    context.close()


def _ready(page: Any, base: str) -> None:
    page.goto(base + "playground/")
    page.locator("#sg-pg-tab-ask").click()
    try:
        page.wait_for_function(
            "document.getElementById('sg-pg-status').textContent.startsWith('Ready')",
            timeout=180_000,
        )
    except Exception:
        reason = page.locator("#sg-pg-unsupported").inner_text()
        pytest.skip(f"the playground did not start (no network for the CDN?): {reason}")


def _text(page: Any, selector: str) -> str:
    return str(page.locator(selector).text_content())


def test_playground_runs_every_tab(pg_page: Any, base: str) -> None:
    page = pg_page
    _ready(page, base)
    # ask: a suggested question, then a profile switch re-renders with the real token count
    page.locator("#sg-pg-chips button", has_text="how many orders were paid last month").click()
    assert "shop.orders" in _text(page, "#sg-pg-ask-out")
    agent_meta = _text(page, "#sg-pg-ask-meta")
    assert "tokens" in agent_meta and "profile agent" in agent_meta
    page.locator("#sg-pg-profile label", has_text="planner").click()
    assert "profile planner" in _text(page, "#sg-pg-ask-meta")
    # join path: a measured route, then a typed refusal; no SQL is made up
    page.locator("#sg-pg-tab-path").click()
    assert "support" in _text(page, "#sg-pg-path-out")
    assert "routes" in _text(page, "#sg-pg-path-badge")
    page.select_option("#sg-pg-from", "shop.orders")
    page.select_option("#sg-pg-to", "shop.events")
    page.locator("#sg-pg-path-form button[type=submit]").click()
    assert "HUB_PIVOT" in _text(page, "#sg-pg-path-badge")
    assert "SQL snippets appear in the context view" in _text(page, "#sg-pg-path-note")
    # lineage: upstream depth 3 of the revenue view reaches the payments table
    page.locator("#sg-pg-tab-lineage").click()
    page.select_option("#sg-pg-subject", "analytics.revenue_by_segment")
    page.fill("#sg-pg-depth", "3")
    page.locator("#sg-pg-lineage-form button[type=submit]").click()
    assert "shop.payments" in _text(page, "#sg-pg-lineage-out")
    page.locator("#sg-pg-panel-lineage label", has_text="downstream").click()
    assert "downstream" in _text(page, "#sg-pg-lineage-out")
    # privacy: every example masks its value; none mode also replaces quoted literals
    page.locator("#sg-pg-tab-privacy").click()
    page.locator("#sg-pg-examples button", has_text="E-mail").click()
    out = _text(page, "#sg-pg-privacy-out")
    assert "@example" not in out and "<email>" in out
    assert "detected as: email" in _text(page, "#sg-pg-privacy-meta")
    assert "[shop.customers]" in _text(page, "#sg-pg-privacy-table")
    page.locator("#sg-pg-examples button", has_text="Quoted literal").click()
    assert "?" in _text(page, "#sg-pg-privacy-out") and "'vip'" not in _text(
        page, "#sg-pg-privacy-out"
    )
    assert "llm_values = none" in _text(page, "#sg-pg-privacy-table-title")
    # only the library's sweep categories are "detected as"; the literal is said in words
    meta = _text(page, "#sg-pg-privacy-meta")
    assert "detected as: email ·" in meta and "1 quoted literal replaced (none mode)" in meta
    assert "literal," not in meta and "email, literal" not in meta
    # override: apply wins, reset restores the site's own view
    page.locator("#sg-pg-tab-override").click()
    page.locator("#sg-pg-override-form button[type=submit]").click()
    before = _text(page, "#sg-pg-before")
    assert "Playground override wins." in _text(page, "#sg-pg-after")
    assert "Playground override wins." not in before
    assert _text(page, "#sg-pg-before-fp") != _text(page, "#sg-pg-after-fp")
    # the lines the overlay changed are marked, and After opens on the first of them
    added = page.locator("#sg-pg-after .sg-pg__line--added")
    assert added.count() >= 1 and page.locator("#sg-pg-before .sg-pg__line--removed").count() >= 1
    assert "Playground override wins." in " ".join(added.all_text_contents())
    assert added.first.evaluate(
        "l => { const p = l.parentElement.getBoundingClientRect(), r = l.getBoundingClientRect();"
        " return r.top >= p.top && r.bottom <= p.bottom; }"
    ), "After did not scroll to the first changed line"
    assert "highlighted" in _text(page, "#sg-pg-ov-summary")
    page.locator("#sg-pg-reset").click()
    assert _text(page, "#sg-pg-after") == before
    assert page.locator("#sg-pg-after .sg-pg__line--added").count() == 0
    # a broken overlay is a SchemaGuru diagnostic, an empty question a plain failure
    page.fill("#sg-pg-yaml", "tables: [1")
    page.locator("#sg-pg-override-form button[type=submit]").click()
    assert _text(page, "#sg-pg-diags").startswith("SG208")
    page.fill("#sg-pg-ov-question", "")
    page.locator("#sg-pg-override-form button[type=submit]").click()
    assert _text(page, "#sg-pg-diags") == "Could not run this: the question is empty."
    # no stale views next to the error
    assert _text(page, "#sg-pg-before") == "" and _text(page, "#sg-pg-after") == ""


def test_playground_without_webassembly_explains(page: Any, base: str) -> None:
    page.add_init_script("delete window.WebAssembly")
    page.goto(base + "playground/")
    block = page.locator("#sg-pg-unsupported")
    block.wait_for(state="visible", timeout=5_000)
    assert "WebAssembly" in block.inner_text()
    assert block.locator("a[href$='getting-started/']").count() >= 1
    assert page.locator("#sg-pg-retry").is_hidden()


def test_hung_download_fails_with_retry(page: Any, base: str) -> None:
    # a request that never answers (not one that fails) still ends in the failure message
    page.add_init_script("window.sgPlaygroundStageTimeoutMs = 1500")
    page.route("**/pyodide.js", lambda route: None)  # never fulfilled
    # the playground starts loading when it scrolls into view; Firefox holds the page's `load`
    # event for that (never answered) script, Chromium and WebKit do not
    page.goto(base + "playground/", wait_until="domcontentloaded")
    page.locator("#sg-pg-tab-ask").click()
    block = page.locator("#sg-pg-unsupported")
    block.wait_for(state="visible", timeout=10_000)
    assert "Pyodide stage failed: no answer after 2 s" in block.inner_text()
    assert page.locator("#sg-pg-retry").is_visible()
    assert page.locator("#sg-pg-progress").is_hidden()


def test_loading_shows_progress(pg_page: Any, base: str) -> None:
    page = pg_page
    page.goto(base + "playground/")
    page.wait_for_function(
        "document.getElementById('sg-pg-status').textContent.startsWith('Step ')"
    )
    assert page.locator("#sg-pg-progress").is_visible()
    assert "of 6:" in _text(page, "#sg-pg-status")
    _ready(page, base)
    assert page.locator("#sg-pg-progress").is_hidden()


def test_pasted_html_is_inert(pg_page: Any, base: str) -> None:
    page = pg_page
    _ready(page, base)
    page.locator("#sg-pg-tab-privacy").click()
    page.fill("#sg-pg-privacy-text", '<img src=x onerror="window.__pwned=1">')
    page.locator("#sg-pg-privacy-form button[type=submit]").click()
    assert "<img" in _text(page, "#sg-pg-privacy-out")
    assert page.evaluate("window.__pwned") is None
    assert page.locator("#sg-playground img[src='x']").count() == 0


@pytest.mark.parametrize("width", [375, 1280])
def test_playground_layout(pg_page: Any, base: str, width: int) -> None:
    page = pg_page
    page.set_viewport_size({"width": width, "height": 900})
    _ready(page, base)
    tops = page.eval_on_selector_all(
        "#sg-playground [role=tab]", "ts => ts.map(t => Math.round(t.getBoundingClientRect().top))"
    )
    assert len(set(tops)) == 1, f"the five tabs are not one row: {tops}"
    for tab in ("ask", "path", "lineage", "privacy", "override"):
        assert page.locator(f"#sg-pg-tab-{tab}").evaluate(
            "t => { const r = t.getBoundingClientRect();"
            " return r.left >= 0 && r.right <= innerWidth; }"
        ), f"tab {tab} is off-screen"
        page.locator(f"#sg-pg-tab-{tab}").click()
        assert page.evaluate("document.documentElement.scrollWidth") <= width, tab
    shots = os.environ.get("SG_SCREENSHOT_DIR")  # optional: a place to look at the page
    if shots:
        page.locator("#sg-pg-tab-ask").click()
        page.screenshot(path=str(Path(shots) / f"playground-{width}.png"), full_page=True)


def test_features_diagram_both_themes(page: Any, base: str) -> None:
    strokes = {}
    for scheme in ("light", "dark"):
        page.emulate_media(color_scheme=scheme)
        page.goto(base + "features/")
        page.evaluate("localStorage.clear()")  # Material remembers a toggled palette
        page.goto(base + "features/")
        strokes[scheme] = page.evaluate(
            "getComputedStyle(document.querySelector('svg.sg-arch rect')).stroke"
        )
    assert strokes["light"] != strokes["dark"]
    page.set_viewport_size({"width": 360, "height": 780})
    page.goto(base + "features/")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
