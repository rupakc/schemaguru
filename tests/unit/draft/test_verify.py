from __future__ import annotations

from collections.abc import Callable
from typing import Any

from schemaguru.diagnostics import DiagnosticBag
from schemaguru.draft import verify_drafts
from schemaguru.model.evidence import Evidence
from schemaguru.model.facts import Fact, Provenance, Status
from schemaguru.model.joins import JoinRef
from schemaguru.model.kinds import FactKind
from schemaguru.model.refs import ObjectRef
from tests.unit.measure.fake_backend import FakeBackend

S = "shop"
ORD = ObjectRef(None, S, "orders")
SRC = "llm:script:m1"


def _draft(kind: Any, subject: Any, value: dict[str, Any]) -> Fact:
    return Fact.make(
        kind,
        subject,
        {**value, "prompt_hash": "sha256:x"},
        Provenance.DRAFTED,
        Status.UNVERIFIED,
        source=SRC,
    )


class ClaimBackend(FakeBackend):
    """FakeBackend whose ``check`` answers each claim with ``verdict_for(claim)``."""

    def __init__(self, conn: Any, verdict_for: Callable[[str], str | None]) -> None:
        super().__init__(conn)
        self.verdict_for = verdict_for
        self.claims: list[str] = []

    def check(self, claims: str) -> list[Evidence]:
        self.claims.append(claims)
        v = self.verdict_for(claims)
        return [] if v is None else [Evidence(claim_text=claims, verdict=v)]


SOFT = {"predicate": "deleted IS NULL OR NOT deleted", "marker_columns": ["deleted"]}


def test_verified_soft_delete_becomes_verified(shop_duckdb: Any) -> None:
    be = ClaimBackend(shop_duckdb[0], lambda c: "PASS")
    f = _draft(FactKind.SOFT_DELETE, ORD, SOFT)
    [out] = verify_drafts([f], be)
    assert out.status is Status.VERIFIED and out.method == "exact"
    assert out.id == f.id and out.provenance is Provenance.DRAFTED
    assert out.evidence is not None and out.evidence.verdict == "PASS"
    assert be.claims == [
        "ROWS shop.orders WHERE deleted IS NULL OR NOT deleted VALUE >=1",
        "ROWS shop.orders WHERE NOT (deleted IS NULL OR NOT deleted) VALUE >=1",
    ]


def test_soft_delete_with_no_live_rows_is_contradicted(shop_duckdb: Any) -> None:
    be = ClaimBackend(shop_duckdb[0], lambda c: "FAIL")
    [out] = verify_drafts([_draft(FactKind.SOFT_DELETE, ORD, SOFT)], be)
    assert out.status is Status.CONTRADICTED


def test_soft_delete_without_deleted_rows_stays_unverified(shop_duckdb: Any) -> None:
    be = ClaimBackend(shop_duckdb[0], lambda c: "FAIL" if "NOT (" in c else "PASS")
    [out] = verify_drafts([_draft(FactKind.SOFT_DELETE, ORD, SOFT)], be)
    assert out.status is Status.UNVERIFIED


def test_missing_check_capability_keeps_unverified(shop_duckdb: Any) -> None:
    be = ClaimBackend(shop_duckdb[0], lambda c: None)  # check returns nothing
    bag = DiagnosticBag()
    facts = [
        _draft(FactKind.SOFT_DELETE, ORD, SOFT),
        _draft(FactKind.ENTITY_TYPE, ORD, {"entity": "order", "key": ["order_id"]}),
    ]
    out = verify_drafts(facts, be, diagnostics=bag)
    assert [f.status for f in out] == [Status.UNVERIFIED, Status.UNVERIFIED]
    assert {d.code for d in bag} == {"SG405"}


def test_check_raising_keeps_unverified(shop_duckdb: Any) -> None:
    def boom(c: str) -> str:
        raise RuntimeError("no checker")

    be = ClaimBackend(shop_duckdb[0], boom)
    [out] = verify_drafts([_draft(FactKind.SOFT_DELETE, ORD, SOFT)], be)
    assert out.status is Status.UNVERIFIED


def test_type_note_and_entity_claims(shop_duckdb: Any) -> None:
    be = ClaimBackend(shop_duckdb[0], lambda c: "PASS")
    col = ObjectRef(None, S, "orders", "created_at")
    tn = _draft(
        FactKind.TYPE_NOTE,
        col,
        {"stored_as": "string", "means": "ISO date", "pattern": "____-__-__%"},
    )
    ent = _draft(
        FactKind.ENTITY_TYPE,
        ObjectRef(None, S, "order_items"),
        {"entity": "order line", "key": ["order_id", "product_id"]},
    )
    out = verify_drafts([tn, ent], be)
    assert [f.status for f in out] == [Status.VERIFIED, Status.VERIFIED]
    assert be.claims == [
        "PATTERN shop.orders.created_at LIKE '____-__-__%' RATE >=99%",
        "UNIQUE shop.order_items.order_id WITH [product_id]",
    ]


def test_unverifiable_parts_untouched(shop_duckdb: Any) -> None:
    be = ClaimBackend(shop_duckdb[0], lambda c: "PASS")
    desc = _draft(FactKind.DESCRIPTION, ORD, {"text": "Orders."})
    tn = _draft(
        FactKind.TYPE_NOTE,
        ObjectRef(None, S, "orders", "created_at"),
        {"stored_as": "string", "means": "date", "pattern": None},
    )
    human = Fact.make(FactKind.SOFT_DELETE, ORD, SOFT, Provenance.HUMAN, Status.UNVERIFIED)
    assert verify_drafts([desc, tn, human], be) == [desc, tn, human]
    assert be.claims == []


def _join_draft(left: str, lc: str, right: str, rc: str) -> Fact:
    j = JoinRef((ObjectRef(None, S, left, lc),), (ObjectRef(None, S, right, rc),))
    from schemaguru.model.joins import JOIN_VALUE_KEYS

    return _draft(FactKind.JOIN, j, {**{k: None for k in JOIN_VALUE_KEYS}, "candidate": True})


def test_join_candidate_measured(shop_duckdb: Any) -> None:
    be = FakeBackend(shop_duckdb[0])
    ok = _join_draft("orders", "customer_id", "customers", "customer_id")
    broken = _join_draft("broken_child", "parent_id", "ref_parent", "id")
    out = verify_drafts([ok, broken], be)
    assert out[0].status is Status.VERIFIED and out[0].value["verdict"] == "OK"
    assert out[0].value["match_rate"] == 1000 / 1020
    assert out[0].value["prompt_hash"] == "sha256:x" and out[0].id == ok.id
    assert out[1].status is Status.CONTRADICTED and out[1].value["verdict"] == "BROKEN"


def test_join_measure_failure_keeps_unverified(shop_duckdb: Any) -> None:
    be = FakeBackend(shop_duckdb[0], fail=lambda r: True)
    bag = DiagnosticBag()
    [out] = verify_drafts(
        [_join_draft("orders", "customer_id", "customers", "customer_id")], be, diagnostics=bag
    )
    assert out.status is Status.UNVERIFIED and [d.code for d in bag] == ["SG405"]
