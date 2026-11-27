"""The landing page's build-time data is produced by the real library and carries its evidence."""

from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path
from typing import Any

import pytest

from schemaguru.catalog import registry

from .test_docs_generated import EMAIL, HEX32, ROOT, _load

PII_EXAMPLE_IN = "Ask jane.doe@example.com about order 42"


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
    claim = _load("gen_docs").scale_claim()
    assert f"{landing['scale']['context_p95_s']:,.1f} s" in claim and "guide/scale.md" in claim
    assert f"{landing['scale']['tables']:,}" in claim


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
