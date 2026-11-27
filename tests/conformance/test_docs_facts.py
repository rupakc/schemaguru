"""Facts the docs state by hand are pinned to their source, so drift fails here.

* the version and the number of catalog readers on the Features page;
* the Python range, from the ``pyproject.toml`` classifiers, on the landing page and README;
* which readers are tested against real engines: one generated statement
  (``docs/generated/readers_tested.md``, from the CI integration job's engine list), included
  by the mkdocs pages and copied verbatim between markers into the README and the wiki, which
  cannot include snippets.
"""

from __future__ import annotations

import re

import pytest

from schemaguru.__about__ import __version__
from schemaguru.catalog.registry import dialects

from .test_docs_generated import ROOT, _load

DOCS = ROOT / "docs"
MARKED = ("README.md", "docs/wiki/Catalog-and-Crawl.md", "docs/wiki/FAQ.md")
SNIPPET_PAGES = ("docs/features.md", "docs/guide/crawl.md")
NUMBER_WORDS = {11: "eleven", 10: "ten", 12: "twelve", 9: "nine", 13: "thirteen", 14: "fourteen"}
MARKER = re.compile(r"<!-- readers-tested:begin -->\n(.*?)\n<!-- readers-tested:end -->", re.S)


def _python_range() -> str:
    # a regex, not tomllib: the CI matrix includes Python 3.10
    versions = re.findall(
        r'"Programming Language :: Python :: (3\.\d+)"',
        (ROOT / "pyproject.toml").read_text("utf-8"),
    )
    versions.sort(key=lambda v: int(v.split(".")[1]))
    return f"Python {versions[0]} to {versions[-1]}"


def _read(rel: str) -> str:
    return (ROOT / rel).read_text("utf-8")


def test_features_states_the_current_version_and_reader_count() -> None:
    text = _read("docs/features.md")
    assert f"in {__version__} (alpha)" in text
    assert f"readers for {NUMBER_WORDS[len(dialects())]} warehouses" in text


def test_python_range_matches_the_classifiers() -> None:
    wanted = _python_range()
    for rel in ("docs/index.md", "README.md"):
        assert wanted in _read(rel), f"{rel} must say {wanted!r}"


def test_the_generated_statement_names_exactly_the_ci_engines() -> None:
    gen = _load("gen_docs")
    live, recorded = gen.tested_readers()
    assert set(live) | set(recorded) == set(dialects()) and not set(live) & set(recorded)
    ci = _read(".github/workflows/ci.yml")
    engines = re.search(r"SCHEMAGURU_REQUIRE_ENGINES:\s*([\w,]+)", ci)
    assert engines is not None
    assert set(engines.group(1).split(",")) <= set(live)
    assert "Synapse is supported through the SQL Server reader and is not tested" in (
        gen.readers_tested()
    )


@pytest.mark.parametrize("rel", SNIPPET_PAGES)
def test_mkdocs_pages_include_the_generated_statement(rel: str) -> None:
    assert '--8<-- "generated/readers_tested.md"' in _read(rel)


@pytest.mark.parametrize("rel", MARKED)
def test_readme_and_wiki_carry_the_same_statement(rel: str) -> None:
    gen = _load("gen_docs")
    found = MARKER.findall(_read(rel))
    assert found == [gen.readers_tested().strip()], f"{rel}: run scripts/gen_docs.py and re-paste"


def test_no_page_claims_synapse_is_tested_in_ci() -> None:
    claim = re.compile(r"Synapse[^\n]*(real engine|tested against|, CI)", re.I)
    pages = [*DOCS.glob("**/*.md"), ROOT / "README.md", DOCS / "overrides" / "home.html"]
    for page in pages:
        if "specs" in page.parts:
            continue
        for line in page.read_text("utf-8").splitlines():
            if claim.search(line) and "not " not in line:
                pytest.fail(f"{page.relative_to(ROOT)}: {line.strip()}")


def test_negative_control_marker_mismatch_is_caught() -> None:
    stale = "<!-- readers-tested:begin -->\nold text\n<!-- readers-tested:end -->"
    assert MARKER.findall(stale) == ["old text"]
    assert _python_range() != "Python 3.9 to 3.13"
