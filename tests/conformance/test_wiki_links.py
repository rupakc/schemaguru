"""The GitHub wiki (``docs/wiki/``, spec 23.3): links resolve, every page has its frame.

A GitHub wiki is a flat set of Markdown pages addressed by file name, so a link resolves when it
names a page (``[[Page]]``, ``[[text|Page]]``, ``[text](Page)``, optionally with ``#anchor``) or
an image/file in ``docs/wiki``. A relative link that leaves the wiki (``../../src``) would work
in the repository view and break in the wiki, so it is refused: repository files are linked by
absolute URL. Every page opens with a three-line summary (a ``>`` block right under the title)
and ends with a ``## See also`` section that links somewhere. The no-proprietary-names scan is
``tests/architecture/test_denylist.py`` (it covers these files like every tracked file).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WIKI = Path(__file__).resolve().parent.parent.parent / "docs" / "wiki"
SPECIAL = frozenset({"_Sidebar", "_Footer"})

# spec 23.3 (+ task 23 decisions): the pages the wiki must hold
REQUIRED = frozenset(
    {
        "Home",
        "Why-Schema-Intelligence",
        "Concepts",
        "Architecture",
        "Catalog-and-Crawl",
        "Store",
        "Measurement",
        "Sampling",
        "Join-Graph",
        "Path-Search-and-Hub-Guard",
        "Waypoint-Rules",
        "Drafting",
        "PII-and-Sampling-Policy",
        "Overlays",
        "Scoping",
        "Rendering",
        "Refresh-and-Drift",
        "Feedback-and-Guard",
        "Server",
        "Agent-Integration",
        "CLI",
        "Configuration",
        "Diagnostics",
        "Scale-and-Cost",
        "Scale",
        "Security",
        "FAQ",
        "Glossary",
        "Design-Decisions",
        "Contributing",
        # one guide per shipped catalog reader
        "Warehouse-BigQuery",
        "Warehouse-Snowflake",
        "Warehouse-Redshift",
        "Warehouse-Databricks",
        "Warehouse-PostgreSQL",
        "Warehouse-DuckDB",
        "Warehouse-ClickHouse",
        "Warehouse-TSQL",
        "Warehouse-Oracle",
        "Warehouse-Trino",
        "Warehouse-MySQL",
        # one page per extension point (spec 21)
        "Extending-CatalogReader",
        "Extending-Store",
        "Extending-Retriever-and-Embedder",
        "Extending-TokenCounter",
        "Extending-LLMProvider",
        "Extending-Detector",
        "Extending-FailureClassifier",
        "Extending-FactKind",
        "Extending-Profile-and-Format",
        "Extending-ChangeClassifier",
        "Extending-JoinCandidateSource",
        "Extending-Authenticator",
    }
)

_WIKILINK = re.compile(r"\[\[([^\]]+)\]\]")
_MDLINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_FENCE = re.compile(r"^(```|~~~)")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def _prose(text: str) -> str:
    """``text`` without fenced code blocks and inline code (links there are examples)."""
    out: list[str] = []
    fenced = False
    for line in text.splitlines():
        if _FENCE.match(line.strip()):
            fenced = not fenced
            continue
        if not fenced:
            out.append(re.sub(r"`[^`]*`", "", line))
    return "\n".join(out)


def link_targets(text: str) -> list[str]:
    """Every wiki-internal link target in ``text`` (external URLs and pure anchors skipped)."""
    prose = _prose(text)
    found: list[str] = []
    for m in _WIKILINK.finditer(prose):
        inner = m.group(1)
        found.append(inner.split("|", 1)[1] if "|" in inner else inner)
    for m in _MDLINK.finditer(prose):
        target = m.group(1)
        if _SCHEME.match(target) or target.startswith("#"):
            continue
        found.append(target)
    return [t.strip() for t in found]


def unresolved(text: str, pages: set[str], files: set[str]) -> list[str]:
    """Targets that name neither a page nor a file of the wiki."""
    bad: list[str] = []
    for target in link_targets(text):
        name = target.split("#", 1)[0]
        if not name:
            continue
        if "/" in name or "\\" in name or name.startswith(".."):
            bad.append(f"{target} (leaves the wiki: link repository files by absolute URL)")
            continue
        stem = name[:-3] if name.endswith(".md") else name
        if stem.replace(" ", "-") not in pages and name not in files:
            bad.append(target)
    return bad


def frame_problems(text: str) -> list[str]:
    """A page's frame: ``# Title``, a three-line ``>`` summary, a final ``## See also``."""
    problems: list[str] = []
    lines = text.splitlines()
    if not lines or not lines[0].startswith("# "):
        problems.append("first line is not a '# Title'")
        return problems
    rest = lines[1:]
    while rest and not rest[0].strip():
        rest = rest[1:]
    summary = []
    for line in rest:
        if not line.startswith(">"):
            break
        summary.append(line)
    if len(summary) != 3 or not all(s[1:].strip() for s in summary):
        problems.append(f"summary block under the title has {len(summary)} lines, not 3")
    headings = [i for i, ln in enumerate(lines) if ln.startswith("## ")]
    see = [i for i in headings if lines[i].strip().lower() == "## see also"]
    if not see:
        problems.append("no '## See also' section")
    else:
        if see[-1] != headings[-1]:
            problems.append("'## See also' is not the last section")
        if not link_targets("\n".join(lines[see[-1] :])):
            problems.append("'## See also' links nowhere")
    return problems


def _pages() -> dict[str, str]:
    return {p.stem: p.read_text(encoding="utf-8") for p in sorted(WIKI.glob("*.md"))}


def _files() -> set[str]:
    return {p.name for p in WIKI.iterdir() if p.is_file()}


# ----- negative controls: each check fails on a page that breaks it -------------------------


def test_negative_control_links() -> None:
    pages = {"Home", "Concepts"}
    good = "See [[Concepts]], [[the home|Home]], [x](Concepts#facts), [y](https://e.org/a)."
    assert unresolved(good, pages, {"diagram.png"}) == []
    assert unresolved("[[Nope]]", pages, set()) == ["Nope"]
    assert unresolved("[a](Missing-Page)", pages, set()) == ["Missing-Page"]
    assert unresolved("[a](../../src/schemaguru/guru.py)", pages, set())
    assert unresolved("![d](diagram.png)", pages, {"diagram.png"}) == []
    # links inside code are examples, not links
    assert unresolved("```\n[[Nope]]\n```\n`[[Nope]]`", pages, set()) == []


def test_negative_control_frame() -> None:
    ok = "# T\n\n> one\n> two\n> three\n\nBody.\n\n## Part\n\n## See also\n\n- [[Home]]\n"
    assert frame_problems(ok) == []
    assert frame_problems("No title\n")
    assert any("2 lines" in p for p in frame_problems("# T\n\n> one\n> two\n\n## See also\n[[H]]"))
    assert any("See also" in p for p in frame_problems("# T\n\n> a\n> b\n> c\n\n## Body\n"))
    late = "# T\n\n> a\n> b\n> c\n\n## See also\n\n[[H]]\n\n## After\n"
    assert any("not the last" in p for p in frame_problems(late))
    assert any("nowhere" in p for p in frame_problems("# T\n\n> a\n> b\n> c\n\n## See also\n"))


# ----- the wiki itself ---------------------------------------------------------------------


def test_required_pages_exist() -> None:
    have = set(_pages())
    assert have >= REQUIRED, sorted(REQUIRED - have)
    assert {"_Sidebar", "_Footer"} <= have


def test_no_unexpected_pages() -> None:
    """A page outside the agreed list must be added to REQUIRED deliberately."""
    extra = set(_pages()) - REQUIRED - SPECIAL
    assert not extra, sorted(extra)


@pytest.mark.parametrize("name", sorted(REQUIRED))
def test_page_frame(name: str) -> None:
    text = _pages().get(name)
    assert text is not None, f"{name}.md is missing"
    assert frame_problems(text) == []


def test_every_link_resolves() -> None:
    pages, files = set(_pages()), _files()
    bad = {name: unresolved(text, pages, files) for name, text in _pages().items()}
    assert {k: v for k, v in bad.items() if v} == {}


def test_sidebar_reaches_every_page() -> None:
    linked = {t.split("#", 1)[0] for t in link_targets(_pages()["_Sidebar"])}
    missing = REQUIRED - {t[:-3] if t.endswith(".md") else t for t in linked}
    assert not missing, sorted(missing)
