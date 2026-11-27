"""Render scripts/social_card/card.html to docs/assets/social-card.png (the og:image).

Run by hand after editing the card (needs Playwright with Chromium: ``pip install playwright
&& playwright install chromium``); the PNG is committed, so the docs build does not need it."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "scripts" / "social_card" / "card.html"
OUT = ROOT / "docs" / "assets" / "social-card.png"
WIDTH, HEIGHT = 1200, 630


def main() -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": WIDTH, "height": HEIGHT})
        page.goto(SOURCE.as_uri())
        page.screenshot(path=str(OUT), full_page=False)
        browser.close()
    print(f"wrote {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
