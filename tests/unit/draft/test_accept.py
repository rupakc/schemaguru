from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from schemaguru.draft import accept, write_accepted
from schemaguru.model.evidence import Evidence
from schemaguru.model.facts import Fact, Provenance, Status
from schemaguru.model.kinds import FactKind
from schemaguru.model.refs import ObjectRef
from schemaguru.store.sqlite import SQLiteStore

ORD = ObjectRef(None, "shop", "orders")
SRC = "llm:script:m1"


def _draft(kind: object, subject: ObjectRef, value: dict[str, object], **kw: object) -> Fact:
    return Fact.make(
        kind,
        subject,
        {**value, "prompt_hash": "sha256:x"},
        Provenance.DRAFTED,
        kw.pop("status", Status.UNVERIFIED),
        source=SRC,
        **kw,
    )


def test_accept_creates_human_fact(shop_store: SQLiteStore) -> None:
    d = _draft(FactKind.DESCRIPTION, ORD, {"text": "One row per order."})
    shop_store.put_facts([d])
    [h] = accept(shop_store, [d.id])
    assert h.provenance is Provenance.HUMAN and h.status is Status.UNVERIFIED
    assert h.source == f"accepted:{d.id}"
    assert dict(h.value) == {"text": "One row per order."}
    stored = shop_store.facts_for([ORD], ["DESCRIPTION"])
    assert h in stored and d in stored  # the draft is kept; precedence picks HUMAN


def test_accept_keeps_verified_evidence(shop_store: SQLiteStore) -> None:
    ev = Evidence(claim_text="ROWS shop.orders WHERE NOT deleted VALUE >=1", verdict="PASS")
    d = _draft(
        FactKind.SOFT_DELETE,
        ORD,
        {"predicate": "NOT deleted", "marker_columns": ["deleted"]},
        status=Status.VERIFIED,
        method="exact",
        evidence=ev,
    )
    shop_store.put_facts([d])
    [h] = accept(shop_store, [d.id])
    assert h.status is Status.VERIFIED and h.evidence == ev


def test_accept_rejects_bad_ids(shop_store: SQLiteStore) -> None:
    human = Fact.make(FactKind.DESCRIPTION, ORD, {"text": "x"}, Provenance.HUMAN, Status.UNVERIFIED)
    err = _draft(FactKind.DESCRIPTION, ORD, {"text": None, "code": "SG401"}, status=Status.ERROR)
    shop_store.put_facts([human, err])
    for bad in ("sha256:nope", human.id, err.id):
        with pytest.raises(ValueError):
            accept(shop_store, [bad])


def test_write_accepted_reports_unwritable(tmp_path: Path, shop_store: SQLiteStore) -> None:
    desc = _draft(FactKind.DESCRIPTION, ORD, {"text": "Orders."})
    pii = _draft(
        FactKind.PII, ObjectRef(None, "shop", "orders", "customer_id"), {"category": "other"}
    )
    shop_store.put_facts([desc, pii])
    humans = accept(shop_store, [desc.id, pii.id])
    out = tmp_path / "schemaguru" / "accepted.yaml"
    unwritten = write_accepted(humans, out)
    assert [f.kind.name for f in unwritten] == ["PII"]
    doc = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert doc["tables"]["shop.orders"]["description"] == "Orders."
