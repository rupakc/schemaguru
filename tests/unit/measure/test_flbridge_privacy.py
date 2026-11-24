"""Spec 11: FactLang evidence is scrubbed by the table's value policy before it is stored."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from schemaguru.config import SamplingConfig
from schemaguru.measure import flbridge
from schemaguru.measure.requests import KeyOverlapReq, TopValues
from schemaguru.model.joins import JoinRef
from schemaguru.model.refs import ObjectRef

COL = ObjectRef(None, "shop", "customers", "email")
INLINED = "SELECT COUNT(*) FROM shop.customers WHERE email NOT IN ('alice@example.com', 'O''Brien')"


def _outcome() -> dict[str, Any]:
    return {
        "claim_text": "VALUES shop.customers.email IN ['alice@example.com']",
        "verdict": "FAIL",
        "sql": INLINED,
        "facts": {
            "undeclared_distinct": 2,
            "top_undeclared": [
                {"value": "bob@example.com", "count": 3},
                {"value": "carol@example.com", "count": 1},
            ],
        },
        "message": "unexpected value bob@example.com",
    }


@dataclass
class FakeEv:
    claim_text: str = "VALUES shop.customers.email IN ['alice@example.com']"
    verdict: str = "FAIL"
    sql: str = INLINED
    params: tuple[Any, ...] = ("alice@example.com", 5)
    dialect: str = "duckdb"
    raw: dict[str, Any] = field(default_factory=_outcome)

    def to_dict(self) -> dict[str, Any]:
        return self.raw


def _ev(mode: str, store_raw: bool = False) -> Any:
    s = SamplingConfig(llm_values=mode, store_raw_samples=store_raw)  # type: ignore[arg-type]
    m = flbridge.evidence_mode(TopValues(COL, 20), s)
    e = FakeEv()
    return flbridge._evidence(e, [e], mode=m)


def test_masked_evidence_has_no_raw_values() -> None:
    ev = _ev("masked")
    d = ev.to_dict()
    assert "example.com" not in repr(d) and "Brien" not in repr(d)
    top = d["raw"]["facts"]["top_undeclared"]
    assert top == [{"value": "<email>", "count": 3}, {"value": "<email>", "count": 1}]
    assert d["params"] == ["<email>", 5]
    assert ev.sql == "SELECT COUNT(*) FROM shop.customers WHERE email NOT IN (?, ?)"
    assert d["raw"]["sql"] == ev.sql


def test_none_evidence_keeps_counts_only() -> None:
    d = _ev("none").to_dict()
    assert "example.com" not in repr(d)
    facts = d["raw"]["facts"]
    assert "top_undeclared" not in facts
    assert facts["top_undeclared_count"] == 2 and facts["undeclared_distinct"] == 2
    assert d["params"] == ["?", 5]


def test_raw_without_store_raw_samples_is_masked_but_with_it_is_raw() -> None:
    assert "example.com" not in repr(_ev("raw").to_dict())
    d = _ev("raw", True).to_dict()
    assert d["raw"]["facts"]["top_undeclared"][0]["value"] == "bob@example.com"
    # an inlined-literal SQL string is never stored, whatever the mode
    assert "alice" not in (d["sql"] or "") and "alice" not in d["raw"]["sql"]


def test_join_takes_the_most_restrictive_side() -> None:
    j = JoinRef(
        (ObjectRef(None, "shop", "orders", "email"),),
        (ObjectRef(None, "crm", "people", "email"),),
    )
    s = SamplingConfig(llm_values="raw", per_glob={"crm.*": "none"}, store_raw_samples=True)
    assert flbridge.evidence_mode(KeyOverlapReq(j), s) == "none"


def test_check_without_a_table_uses_the_most_restrictive_configured_mode() -> None:
    s = SamplingConfig(llm_values="raw", per_glob={"hr.*": "none"}, store_raw_samples=True)
    assert flbridge.evidence_mode(None, s) == "none"
