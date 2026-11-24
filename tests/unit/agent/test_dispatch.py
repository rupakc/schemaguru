"""``handle_tool_call`` (spec 18.1, 11): JSON with a ``text`` field, never raises, value policy."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from schemaguru.agent import handle_tool_call, prompt_block
from schemaguru.config import Budget
from schemaguru.guru import Guru
from schemaguru.measure.backend import CostEstimate, MeasureResult
from schemaguru.measure.requests import Request
from schemaguru.model.evidence import Evidence
from schemaguru.model.refs import ObjectRef
from schemaguru.model.structure import Schema, Table
from tests.fixtures.shop import ShopFacts
from tests.unit.agent.conftest import CheckingBackend, make_shop_guru
from tests.unit.agent.planted import SECRETS, leaks


def call(guru: Guru, name: str, args: Any) -> dict[str, Any]:
    out = handle_tool_call(guru, name, args)
    assert json.loads(json.dumps(out)) == out  # JSON-safe, round-trips
    assert isinstance(out["text"], str)
    return out


def test_dispatch_roundtrip_each_tool(shop_guru: Guru, seam_check: Any) -> None:
    ctx = call(shop_guru, "schema_context", {"question": "refund rate by month"})
    assert "[shop.orders]" in ctx["text"] and "shop.orders" in ctx["tables"]
    assert ctx["fingerprint"] == shop_guru.context("refund rate by month").fingerprint
    hits = call(shop_guru, "search_tables", {"query": "orders", "k": 3})
    assert hits["tables"][0]["table"] == "shop.orders"
    assert "One row per order placed" in hits["tables"][0]["description"]
    assert len(hits["tables"]) <= 3
    desc = call(shop_guru, "describe_table", {"table": "orders"})
    assert desc["table"] == "shop.orders" and "[shop.orders]" in desc["text"]
    path = call(shop_guru, "find_join_path", {"from": "orders", "to": "customers"})
    assert path["refusal"] is None and path["paths"] and "shop.orders" in path["text"]
    vals = call(shop_guru, "column_values", {"column": "shop.orders.status"})
    assert vals["column"] == "shop.orders.status" and vals["mode"] == "masked"
    assert "'paid'" in vals["text"] and "'refunded'" in vals["text"]
    assert any(e["provenance"] == "HUMAN" for e in vals["entries"])
    term = call(shop_guru, "explain_term", {"term": "Refund Rate"})
    assert "refunded orders over all orders" in term["text"]
    assert term["entries"][0]["maps_to"] == ["shop.orders"]
    assert call(shop_guru, "explain_term", {"term": "nothing"})["entries"] == []
    # the façade delegates
    assert shop_guru.handle_tool_call("describe_table", {"table": "orders"}) == desc


@pytest.mark.parametrize(
    ("name", "args", "needle"),
    [
        ("nope", {}, "unknown tool"),
        ("describe_table", {}, "missing required argument 'table'"),
        ("describe_table", {"table": 3}, "'table' must be a string"),
        ("describe_table", {"table": "orders", "extra": 1}, "unexpected argument 'extra'"),
        ("search_tables", {"query": "x", "k": "many"}, "'k' must be an integer"),
        ("search_tables", {"query": "x", "k": True}, "'k' must be an integer"),
        ("search_tables", {"query": "x", "k": 0}, "'k' must be >= 1"),
        ("search_tables", {"query": "x", "k": 1000}, "'k' must be <= 100"),
        ("describe_table", {"table": "nowhere"}, "unknown table"),
        ("column_values", {"column": "orders"}, "not a column reference"),
        ("schema_context", "not a mapping", "arguments must be an object"),
        ("check_fact", {"claims": "ROWS shop.orders VALUE >=1"}, "not available"),
    ],
)
def test_dispatch_bad_args_error_not_raise(
    shop_guru: Guru, no_seam_check: Any, name: str, args: Any, needle: str
) -> None:
    out = call(shop_guru, name, args)
    assert out["error"]["code"] == "SG803"
    assert needle in out["error"]["message"]
    assert out["text"].startswith("SG803 ")


def test_ambiguous_bare_name_lists_candidates_never_guesses(shop_guru: Guru) -> None:
    shop_guru.store.upsert_schemas([Schema(None, "archive")])
    shop_guru.store.upsert_tables([Table(ObjectRef(None, "archive", "orders"), "table")])
    out = call(shop_guru, "describe_table", {"table": "orders"})
    assert out["error"]["code"] == "SG803"
    assert out["error"]["candidates"] == ["archive.orders", "shop.orders"]
    assert "table" not in out


def test_unexpected_exception_returns_sg804_with_masked_message(
    tmp_path: Path, shop_duckdb: tuple[Any, ShopFacts], seam_check: Any
) -> None:
    boom = RuntimeError("warehouse said no for a1@example.com")
    g = make_shop_guru(tmp_path, shop_duckdb[0], backend=CheckingBackend(shop_duckdb[0], boom))
    try:
        out = call(g, "check_fact", {"claims": "ROWS shop.orders VALUE >=1"})
        assert out["error"]["code"] == "SG804"
        assert out["error"]["type"] == "RuntimeError"
        assert "a1@example.com" not in json.dumps(out) and "<email>" in out["error"]["message"]
    finally:
        g.close()


def test_handler_raising_anything_is_caught(shop_guru: Guru, monkeypatch: Any) -> None:
    def explode(*a: Any, **k: Any) -> Any:
        raise ZeroDivisionError("x")

    monkeypatch.setattr(shop_guru, "find_path", explode)
    out = call(shop_guru, "find_join_path", {"from": "orders", "to": "customers"})
    assert out["error"] == {"code": "SG804", "type": "ZeroDivisionError", "message": "x"}


# --- check_fact: spend under the budget, ledgered ------------------------------------------


def _evidence(nbytes: int | None = 1000) -> Evidence:
    return Evidence(claim_text="ROWS shop.orders VALUE >=1", verdict="pass", bytes_processed=nbytes)


def test_check_fact_runs_through_budget_and_ledger(
    tmp_path: Path, shop_duckdb: tuple[Any, ShopFacts], seam_check: Any
) -> None:
    backend = CheckingBackend(shop_duckdb[0], [_evidence(1000)])
    g = make_shop_guru(tmp_path, shop_duckdb[0], backend=backend)
    try:
        out = call(g, "check_fact", {"claims": "ROWS shop.orders VALUE >=1"})
        assert "error" not in out and out["evidence"][0]["verdict"] == "pass"
        assert "pass" in out["text"]
        assert backend.checked == ["ROWS shop.orders VALUE >=1"]
        assert g.store.ledger_total("agent_check", "queries", None) == 1
        assert g.store.ledger_total("agent_check", "bytes", None) == 1000
        # unknown spend is ledgered pessimistically, never as 0 bytes
        backend.evidence = [_evidence(None)]
        call(g, "check_fact", {"claims": "ROWS shop.orders VALUE >=1"})
        assert g.store.ledger_total("agent_check.unknown_bytes", "queries", None) == 1
        assert g.store.ledger_total("agent_check", "bytes", None) == 1000
    finally:
        g.close()


def test_check_fact_refused_over_budget_without_running(
    tmp_path: Path, shop_duckdb: tuple[Any, ShopFacts], seam_check: Any
) -> None:
    backend = CheckingBackend(shop_duckdb[0], [_evidence()])
    g = make_shop_guru(tmp_path, shop_duckdb[0], backend=backend)
    try:
        g.config = replace(g.config, budget=Budget(max_bytes_total=0))
        out = call(g, "check_fact", {"claims": "ROWS shop.orders VALUE >=1"})
        assert out["error"]["code"] == "SG302"
        assert backend.checked == []
        assert g.store.ledger_total("agent_check", "queries", None) == 0
    finally:
        g.close()


def test_check_fact_denied_by_config(
    tmp_path: Path, shop_duckdb: tuple[Any, ShopFacts], seam_check: Any
) -> None:
    backend = CheckingBackend(shop_duckdb[0], [_evidence()])
    g = make_shop_guru(tmp_path, shop_duckdb[0], backend=backend)
    try:
        g.config = replace(g.config, agent_checks="deny")
        out = call(g, "check_fact", {"claims": "ROWS shop.orders VALUE >=1"})
        assert out["error"]["code"] == "SG803" and "agent_checks" in out["error"]["message"]
        assert backend.checked == []
    finally:
        g.close()


# --- privacy: every planted secret through every tool, masked and none ------------------------


class _EvidenceBackend:
    """A backend whose check echoes personal data in every evidence field."""

    def capabilities(self) -> frozenset[str]:
        return frozenset()

    def measure(self, reqs: Sequence[Request]) -> list[MeasureResult]:
        return []

    def estimate(self, reqs: Sequence[Request]) -> list[CostEstimate]:
        return []

    def check(self, claims: str) -> list[Evidence]:
        return [
            Evidence(
                claim_text="VALUES shop.customers.email IN ['q17@example.com']",
                verdict="fail",
                sql="SELECT 1 FROM shop.customers WHERE email = 'q17@example.com'",
                params=("q17@example.com",),
                bytes_processed=10,
                raw={"offending": [{"value": "q17@example.com", "count": 1}]},
            )
        ]


CALLS: list[tuple[str, dict[str, Any]]] = [
    ("schema_context", {"question": "customers email phone segment vip payments"}),
    ("schema_context", {"question": "customers", "budget_tokens": 200}),
    ("describe_table", {"table": "shop.customers"}),
    ("describe_table", {"table": "shop.payments"}),
    ("describe_table", {"table": "shop.accounts"}),
    ("column_values", {"column": "shop.customers.email"}),
    ("column_values", {"column": "shop.customers.phone"}),
    ("column_values", {"column": "shop.customers.segment"}),
    ("search_tables", {"query": "customers segment owner orders"}),
    ("explain_term", {"term": "vip"}),
    ("find_join_path", {"from": "shop.payments", "to": "shop.orders"}),
    ("check_fact", {"claims": "VALUES shop.customers.email IN ['q17@example.com']"}),
]


@pytest.mark.parametrize("mode", ["masked", "none"])
def test_no_planted_secret_leaves_through_any_tool(
    planted: Callable[..., Guru], seam_check: Any, mode: str
) -> None:
    g = planted(mode, backend=_EvidenceBackend())
    out: list[tuple[str, str]] = []
    for fmt in ("text", "json", "xml", "markdown"):
        out.append((f"context/{fmt}", str(g.context("customers email phone vip", format=fmt))))
    out.append(("prompt_block", prompt_block(g, "customers vip")))
    for name, args in CALLS:
        res = handle_tool_call(g, name, args)
        assert "error" not in res, (name, res)
        out.append((f"{name}{args}", json.dumps(res, ensure_ascii=False)))
    found = {(where, s) for where, text in out for s in leaks(text)}
    assert not found, "\n".join(f"{s} ({SECRETS[s]}) in {where}" for where, s in sorted(found))


def test_none_mode_column_values_show_counts_only(planted: Callable[..., Guru]) -> None:
    g = planted("none")
    out = handle_tool_call(g, "column_values", {"column": "shop.customers.segment"})
    assert out["mode"] == "none"
    for e in out["entries"]:
        assert "values" not in e and "observed" not in e
    assert any("observed_count" in e or "declared_count" in e for e in out["entries"])
    assert "retail" not in out["text"]


def test_masked_mode_pii_column_values_are_whole_tokens(planted: Callable[..., Guru]) -> None:
    g = planted("masked")
    out = handle_tool_call(g, "column_values", {"column": "shop.customers.phone"})
    shown = [v for e in out["entries"] for v in e.get("observed", [])]
    assert shown and all(o["value"] in ("<email>", "<masked>", "<phone>", "<name>") for o in shown)
    assert "Zebulon" not in json.dumps(out)


def test_raw_mode_is_the_explicit_opt_in(planted: Callable[..., Guru]) -> None:
    g = planted("raw")
    out = handle_tool_call(g, "column_values", {"column": "shop.customers.segment"})
    assert "note l12@example.com" in json.dumps(out)


# --- each privacy layer on its own (the layers overlap, so the sweep above cannot tell) --------


def test_free_text_is_name_masked_not_only_swept(planted: Callable[..., Guru]) -> None:
    """A person's name has no shape the outbound sweep knows; mask_free_text must catch it."""
    from schemaguru.model.kinds import FactKind as F
    from schemaguru.overlays.schema import GLOBAL_SUBJECT
    from tests.golden.shop_knowledge import human, t

    g = planted("masked")
    g.store.put_facts(
        [
            human(F.GLOSSARY, GLOBAL_SUBJECT, {"term": "steward", "definition": "Mary Smith"}),
            human(F.DESCRIPTION, t("payments"), {"text": "payments, owner John Miller"}),
        ]
    )
    term = handle_tool_call(g, "explain_term", {"term": "steward"})
    assert "Mary" not in json.dumps(term) and "<name>" in term["entries"][0]["definition"]
    hits = handle_tool_call(g, "search_tables", {"query": "payments"})
    assert "Miller" not in json.dumps(hits)


def test_none_mode_check_fact_evidence_has_no_values(
    planted: Callable[..., Guru], seam_check: Any
) -> None:
    """``none`` hides every value, also ones with no personal shape (the sweep cannot)."""

    class Backend(_EvidenceBackend):
        def check(self, claims: str) -> list[Evidence]:
            return [
                Evidence(
                    claim_text="VALUES shop.customers.segment IN ['retail']",
                    verdict="fail",
                    sql="SELECT 1 FROM shop.customers WHERE segment = 'retail'",
                    params=("retail",),
                    raw={"offending": [{"value": "wholesale", "count": 1}]},
                )
            ]

    out = handle_tool_call(planted("none", backend=Backend()), "check_fact", {"claims": "x"})
    dumped = json.dumps(out)
    assert "error" not in out and "retail" not in dumped and "wholesale" not in dumped


def test_outbound_sweeps_every_string_but_the_fingerprint() -> None:
    from schemaguru.agent.dispatch import _outbound_tree

    fp = "ab" * 32
    tree = {"text": "x a@b.example", "fingerprint": fp, "rows": [{"d": "call +4915112345678"}]}
    out = _outbound_tree(tree, "masked")
    assert out == {"text": "x <email>", "fingerprint": fp, "rows": [{"d": "call <phone>"}]}
    assert _outbound_tree(tree, "raw") == tree
