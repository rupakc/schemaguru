"""The landing page's build-time data is produced by the real library and carries its evidence."""

from __future__ import annotations

import json
import logging
import re
import tempfile
from pathlib import Path
from typing import Any

import pytest

from schemaguru.catalog import registry

from .test_docs_generated import EMAIL, HEX32, ROOT, _load

PII_EXAMPLE_IN = _load("gen_docs").PII_EXAMPLE_IN
HOME = ROOT / "docs" / "overrides" / "home.html"
INDEX = ROOT / "docs" / "index.md"


@pytest.fixture(scope="module")
def landing_text() -> str:
    pytest.importorskip("duckdb")
    return _load("gen_docs").generate()["generated/landing.json"]


@pytest.fixture(scope="module")
def landing(landing_text: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(landing_text)
    return data


@pytest.fixture(scope="module")
def shop_counts() -> dict[str, int]:
    pytest.importorskip("duckdb")
    shop_docs = _load("_shop_docs")
    with tempfile.TemporaryDirectory() as d, shop_docs.shop_guru(Path(d)) as shop:
        counts: dict[str, int] = shop_docs.naive_counts(shop.conn)
        return counts


def test_landing_traps_are_evidenced(landing: dict[str, Any]) -> None:
    ids = [t["id"] for t in landing["traps"]]
    assert ids == ["hub", "soft_delete", "case_drift", "broken_join"]
    after = "\n".join(line["text"] for line in landing["after_lines"])
    for t in landing["traps"]:
        assert t["evidence"] and t["naive"] and t["title"]
        if t["source"] == "context":
            assert t["evidence"] in after
            assert any(line["trap"] == t["id"] for line in landing["after_lines"])
        else:
            assert t["source"] == "find_join_path"


def test_landing_evidence_names_a_real_refusal(landing: dict[str, Any]) -> None:
    by_id = {t["id"]: t for t in landing["traps"]}
    assert "hub shop.accounts" in by_id["hub"]["evidence"]
    assert "broken" in by_id["broken_join"]["evidence"]


def test_landing_counts_match_the_warehouse(
    landing: dict[str, Any], shop_counts: dict[str, int]
) -> None:
    by_id = {t["id"]: t for t in landing["traps"]}
    assert f"{shop_counts['orders_x_accounts_x_events']:,}" in by_id["hub"]["naive"]
    assert f"{shop_counts['not_deleted']:,}" in by_id["soft_delete"]["naive"]
    assert f"{shop_counts['live']:,}" in by_id["soft_delete"]["naive"]
    assert f"{shop_counts['status_paid_exact']:,}" in by_id["case_drift"]["naive"]
    assert f"{shop_counts['status_paid_any_case']:,}" in by_id["case_drift"]["naive"]


def test_landing_before_is_names_only_and_smaller_than_the_dump(landing: dict[str, Any]) -> None:
    assert landing["question"] == "monthly revenue by customer segment"
    assert "(measured" not in landing["before"] and "⚠" not in landing["before"]
    assert landing["before_tokens"] > 0 and landing["after_tokens"] > 0
    assert landing["schema_dump_tokens"] > 0


def test_landing_lists_every_reader(landing: dict[str, Any]) -> None:
    assert landing["readers"] == sorted(registry.dialects()) and len(landing["readers"]) == 11
    assert len(landing["profiles"]) == 4 and landing["tools"]


def test_landing_scale_comes_from_the_benchmark(landing: dict[str, Any]) -> None:
    result = json.loads((ROOT / "benchmarks" / "results" / "0.2.0.json").read_text("utf-8"))
    assert landing["scale"] == {
        "tables": result["size"]["tables"],
        "context_p95_s": round(result["metrics"]["context_p95_ms"] / 1000, 1),
    }
    gen = _load("gen_docs")
    claim = gen.scale_claim()
    assert f"{landing['scale']['context_p95_s']:,.1f} s" in claim and "guide/scale.md" in claim
    assert f"{landing['scale']['tables']:,}" in claim
    # the target is read from the benchmark's own known limit, and pinned here
    assert gen.context_target_ms() == 200 and "its 200 ms target" in claim


def test_context_target_missing_from_the_benchmark_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    gen = _load("gen_docs")
    monkeypatch.setattr(gen, "_benchmark", lambda: {"known_limits": ["context is slow"]})
    with pytest.raises(ValueError, match="target"):
        gen.context_target_ms()


def test_trap_evidence_is_checked_against_its_own_result() -> None:
    gen = _load("gen_docs")
    traps = [
        {"id": "hub", "evidence": "pivots through hub"},
        {"id": "broken_join", "evidence": "broken joins are refused"},
    ]
    own = {"hub": "every route pivots through hub x", "broken_join": "broken joins are refused"}
    gen.check_trap_evidence(traps, own)
    # negative control: the hub's evidence present only in the broken pair's result is rejected
    swapped = {"hub": "broken joins are refused", "broken_join": "pivots through hub x"}
    with pytest.raises(ValueError, match="hub"):
        gen.check_trap_evidence(traps, swapped)


def test_landing_holds_no_raw_identifying_values(
    landing: dict[str, Any], landing_text: str
) -> None:
    assert landing["pii_example"]["in"] == PII_EXAMPLE_IN
    assert "<email>" in landing["pii_example"]["out"]
    assert not EMAIL.search(landing_text.replace(PII_EXAMPLE_IN, ""))
    assert not HEX32.search(landing_text)
    assert landing["value_policy"] == "masked"


def test_on_env_without_landing_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    hooks = _load("mkdocs_hooks")
    monkeypatch.setattr(hooks, "LANDING", tmp_path / "landing.json")
    env: Any = type("Env", (), {"globals": {}})()
    with caplog.at_level(logging.WARNING):
        hooks.on_env(env, None, None)
    assert caplog.records and "landing.json" in caplog.text
    (tmp_path / "landing.json").write_text('{"question": "q"}', encoding="utf-8")
    hooks.on_env(env, None, None)
    assert env.globals["landing"] == {"question": "q"}


# ----- the home template and the integration tabs ------------------------------------------


def strip_jinja_tags_and_attrs(html: str) -> str:
    """The visible copy a template author typed: no Jinja, no <style>/<svg>, no tags."""
    text = re.sub(r"\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\}", " ", html, flags=re.S)
    text = re.sub(r"<(style|svg|script)\b.*?</\1>", " ", text, flags=re.S | re.I)
    return re.sub(r"<[^>]*>", " ", text)


def test_home_template_has_no_literal_numbers() -> None:
    text = strip_jinja_tags_and_attrs(HOME.read_text(encoding="utf-8"))
    assert not re.search(r"\d{2,}|\d[.,]\d|%", text), "numbers must come from landing.json"


def test_negative_control_literal_number() -> None:
    assert re.search(r"\d{2,}", strip_jinja_tags_and_attrs("<p>saves 42 tokens</p>"))
    assert not re.search(r"\d{2,}", strip_jinja_tags_and_attrs("<p>{{ landing.x }} rows</p>"))


def test_home_sections_in_order() -> None:
    ids = re.findall(r'<section[^>]*id="([^"]+)"', HOME.read_text(encoding="utf-8"))
    assert ids == [
        "sg-hero",
        "sg-before-after",
        "sg-benefits",
        "sg-how",
        "sg-warehouses",
        "sg-trust",
        "sg-cta",
    ]


def test_home_sections_are_labelled_reveals() -> None:
    html = HOME.read_text(encoding="utf-8")
    sections = re.findall(r"<section\b[^>]*>", html)
    assert len(sections) == 7
    for tag in sections:
        assert 'class="sg-section sg-reveal' in tag and "aria-labelledby=" in tag, tag
    for target in re.findall(r'aria-labelledby="([^"]+)"', " ".join(sections)):
        assert f'id="{target}"' in html, target


def test_home_ctas_and_install_chip() -> None:
    html = HOME.read_text(encoding="utf-8")
    assert "{{ 'playground/' | url }}" in html and "Try the playground" in html
    assert "{{ 'getting-started/' | url }}" in html and "Get started" in html
    assert re.search(r'<button[^>]*data-copy="pip install schemaguru"', html)
    assert "{{ 'guide/scale/' | url }}" in html


def test_home_draws_traps_and_readers_from_landing() -> None:
    html = HOME.read_text(encoding="utf-8")
    assert 'data-trap="{{ line.trap }}"' in html and "landing.after_lines" in html
    assert "landing.before" in html and "landing.traps" in html
    assert "landing.before_tokens" in html and "landing.after_tokens" in html
    assert "landing.readers" in html and 'class="sg-badge' in html
    svg = re.search(r'<svg class="sg-flow"[^>]*>.*?</svg>', html, re.S)
    assert svg and 'role="img"' in svg.group(0) and "<title" in svg.group(0)


def test_no_token_saving_claim() -> None:
    """The measured view is larger than a raw dump on the shop: the pitch is correctness."""
    saving = re.compile(
        r"(sav|fewer|less|reduc|cut|smaller|cheaper)\w*\W+(\w+\W+){0,4}tokens?", re.I
    )
    for page in (HOME, INDEX):
        assert not saving.search(page.read_text(encoding="utf-8")), page.name
    assert saving.search("it saves you tokens")  # negative control


def test_index_integration_tabs() -> None:
    text = INDEX.read_text(encoding="utf-8")
    assert re.search(r"^template: home\.html$", text, re.M)
    labels = re.findall(r'<button[^>]*role="tab"[^>]*>([^<]+)</button>', text)
    assert labels == [
        "Python",
        "MCP",
        "CLI",
        "LangChain / LlamaIndex / OpenAI Agents",
        "HTTP server",
    ]
    for tab in re.findall(r'<button[^>]*role="tab"[^>]*>', text):
        assert "aria-selected=" in tab and "aria-controls=" in tab, tab
    for panel in re.findall(r'aria-controls="([^"]+)"', text):
        assert re.search(rf'<div[^>]*id="{panel}"[^>]*role="tabpanel"', text), panel
    assert re.search(r'role="tablist"[^>]*aria-label="', text)
    assert "```python" in text and "## Install" in text
    for command in (" mcp", " context ", " path ", " serve"):
        assert command in text, command
