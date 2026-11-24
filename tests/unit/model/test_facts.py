from datetime import datetime, timezone

import pytest

from schemaguru.model.evidence import Evidence
from schemaguru.model.facts import Fact, Provenance, Status
from schemaguru.model.joins import JoinRef
from schemaguru.model.kinds import FactKind
from schemaguru.model.refs import ObjectRef

ORD = ObjectRef(None, "shop", "orders", "customer_id")
CUS = ObjectRef(None, "shop", "customers", "id")
JOIN = JoinRef((ORD,), (CUS,))
NOW = datetime(2026, 11, 24, tzinfo=timezone.utc)


def test_fact_naive_timestamp_rejected() -> None:
    with pytest.raises(ValueError):
        Fact.make(
            FactKind.ROW_COUNT,
            ORD.table_ref(),
            {"rows": 1},
            Provenance.CATALOG,
            Status.OBSERVED,
            observed_at=datetime(2026, 1, 1),
        )


def test_measured_requires_method() -> None:
    with pytest.raises(ValueError):
        Fact.make(FactKind.NULL_RATE, ORD, {"rate": 0.1}, Provenance.MEASURED, Status.OBSERVED)
    Fact.make(
        FactKind.NULL_RATE, ORD, {"rate": 0.1}, Provenance.MEASURED, Status.OBSERVED, method="exact"
    )


def test_estimated_requires_sampled_method() -> None:
    for method in (None, "exact"):
        with pytest.raises(ValueError):
            Fact.make(
                FactKind.NULL_RATE, ORD, {"rate": 0.1}, Provenance.MEASURED, Status.ESTIMATED,
                method=method,
            )  # fmt: skip
    f = Fact.make(
        FactKind.NULL_RATE, ORD, {"rate": 0.1}, Provenance.MEASURED, Status.ESTIMATED,
        method="sampled:system:0.01:7",
    )  # fmt: skip
    assert f.status is Status.ESTIMATED


def test_verified_rules() -> None:
    kw = {"value": {"columns": ["id"]}}
    with pytest.raises(ValueError):  # sampled never verifies
        Fact.make(
            FactKind.PRIMARY_KEY, CUS.table_ref(), kw["value"], Provenance.MEASURED,
            Status.VERIFIED, method="sampled:keyhash:0.1:1",
        )  # fmt: skip
    with pytest.raises(ValueError):  # human verified needs evidence
        Fact.make(
            FactKind.PRIMARY_KEY, CUS.table_ref(), kw["value"], Provenance.HUMAN, Status.VERIFIED
        )
    Fact.make(
        FactKind.PRIMARY_KEY, CUS.table_ref(), kw["value"], Provenance.MEASURED, Status.VERIFIED,
        method="exact",
    )  # fmt: skip
    Fact.make(
        FactKind.PRIMARY_KEY, CUS.table_ref(), kw["value"], Provenance.HUMAN, Status.VERIFIED,
        evidence=Evidence(claim_text="unique(id)", verdict="PASS"),
    )  # fmt: skip


def test_fact_id_stable_across_remeasure() -> None:
    a = Fact.make(
        FactKind.JOIN, JOIN, {"match_rate": 0.99}, Provenance.MEASURED, Status.OBSERVED,
        method="exact",
    )  # fmt: skip
    b = Fact.make(
        FactKind.JOIN, JOIN, {"match_rate": 0.42, "coverage": 0.1}, Provenance.MEASURED,
        Status.OBSERVED, method="exact", observed_at=NOW,
    )  # fmt: skip
    assert a.id == b.id and a.id.startswith("sha256:")
    c = Fact.make(FactKind.JOIN, JOIN, {}, Provenance.HUMAN, Status.UNVERIFIED)
    assert c.id != a.id  # provenance is part of identity


def test_identity_fields_per_kind() -> None:
    def vals(v: dict[str, object]) -> Fact:
        return Fact.make(
            FactKind.VALUES, ORD, v, Provenance.MEASURED, Status.OBSERVED, method="exact"
        )

    assert (
        vals({"case_sensitive": True, "observed": [1]}).id
        == vals({"case_sensitive": True, "observed": [2]}).id
    )
    assert vals({"case_sensitive": True}).id != vals({"case_sensitive": False}).id
    d1 = Fact.make(FactKind.DESCRIPTION, ORD, {"text": "a"}, Provenance.HUMAN, Status.UNVERIFIED)
    d2 = Fact.make(FactKind.DESCRIPTION, ORD, {"text": "b"}, Provenance.HUMAN, Status.UNVERIFIED)
    assert d1.id != d2.id  # meaning kinds hash the whole value


def test_subject_shape_checked() -> None:
    with pytest.raises(ValueError):
        Fact.make(FactKind.JOIN, ORD, {}, Provenance.CATALOG, Status.UNVERIFIED)
    with pytest.raises(ValueError):
        Fact.make(FactKind.NULL_RATE, ORD.table_ref(), {}, Provenance.MEASURED, Status.OBSERVED,
                  method="exact")  # fmt: skip


def test_roundtrip_with_evidence() -> None:
    f = Fact.make(
        FactKind.JOIN, JOIN, {"match_rate": 1.0, "verdict": "OK"}, Provenance.MEASURED,
        Status.VERIFIED, method="exact", observed_at=NOW, source="x", snapshot_id="s1",
        evidence=Evidence(claim_text="c", params=(1, "a"), bytes_processed=5, raw={"k": [1]}),
    )  # fmt: skip
    assert Fact.from_dict(f.to_dict()) == f


def test_freshness_id_per_column_stable_across_remeasure() -> None:
    t = ORD.table_ref()

    def fr(col: str, age: int) -> Fact:
        return Fact.make(
            FactKind.FRESHNESS, t, {"column": col, "age_seconds": age},
            Provenance.MEASURED, Status.OBSERVED, method="exact",
        )  # fmt: skip

    assert fr("created_at", 1).id != fr("updated_at", 1).id
    assert fr("created_at", 1).id == fr("created_at", 99).id


def test_join_id_includes_cast_to() -> None:
    cast = JoinRef((ORD,), (CUS,), cast_to="string")

    def mk(j: JoinRef) -> Fact:
        return Fact.make(FactKind.JOIN, j, {}, Provenance.MEASURED, Status.OBSERVED, method="exact")

    assert mk(JOIN).id != mk(cast).id
    assert mk(cast).id == mk(JoinRef((ORD,), (CUS,), cast_to="string")).id
    assert cast.canonical() == JOIN.canonical()


def test_sampled_never_verified_any_provenance() -> None:
    for prov in (Provenance.HUMAN, Provenance.DECLARED, Provenance.MEASURED):
        with pytest.raises(ValueError):
            Fact.make(
                FactKind.PRIMARY_KEY, CUS.table_ref(), {"columns": ["id"]}, prov, Status.VERIFIED,
                method="sampled:keyhash:0.1:1", evidence=Evidence(verdict="PASS"),
            )  # fmt: skip
