import re
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from schemaguru.knowledge.resolve import resolve
from schemaguru.model.facts import Fact, Provenance, Status, Waiver
from schemaguru.model.joins import JoinRef
from schemaguru.model.kinds import FactKind
from schemaguru.model.refs import ObjectRef

NOW = datetime(2026, 11, 24, tzinfo=timezone.utc)
TBL = ObjectRef(None, "shop", "orders")
STATUS = ObjectRef(None, "shop", "orders", "status")


def _ago(days: float) -> datetime:
    return NOW - timedelta(days=days)


def _key(kind: object, subj: ObjectRef) -> tuple[str, str]:
    return (kind.name, subj.canonical())  # type: ignore[attr-defined]


def test_human_beats_drafted_meaning() -> None:
    draft = Fact.make(
        FactKind.DESCRIPTION,
        TBL,
        {"text": "draft"},
        Provenance.DRAFTED,
        Status.UNVERIFIED,
        observed_at=_ago(1),
    )
    human = Fact.make(
        FactKind.DESCRIPTION,
        TBL,
        {"text": "human"},
        Provenance.HUMAN,
        Status.UNVERIFIED,
        observed_at=_ago(30),
    )
    eff = resolve([draft, human], now=NOW, ttl_days=14)[_key(FactKind.DESCRIPTION, TBL)]
    assert eff.primary is not None and eff.primary.id == human.id
    assert {f.id for f in eff.all} == {draft.id, human.id}


def test_expectation_kept_and_contradiction_shown() -> None:
    exp = Fact.make(
        FactKind.VALUES,
        STATUS,
        {"values": ["a", "b"], "case_sensitive": True, "split": None},
        Provenance.HUMAN,
        Status.UNVERIFIED,
    )
    obs = Fact.make(
        FactKind.VALUES, STATUS,
        {
            "case_sensitive": True, "split": None,
            "observed": [{"value": "a", "count": 5}, {"value": "c", "count": 412}],
        },
        Provenance.MEASURED, Status.OBSERVED, method="exact", observed_at=_ago(1),
    )  # fmt: skip
    eff = resolve([obs, exp], now=NOW, ttl_days=14)[_key(FactKind.VALUES, STATUS)]
    assert eff.expectation is not None and eff.expectation.id == exp.id
    assert eff.primary is not None and eff.primary.id == exp.id
    assert eff.observed is not None and eff.observed.id == obs.id
    assert len(eff.contradictions) == 1
    assert "c (412 rows)" in eff.contradictions[0] and "a, b" in eff.contradictions[0]


def test_declared_values_not_contradicted_when_observed_within() -> None:
    exp = Fact.make(
        FactKind.VALUES,
        STATUS,
        {"values": ["a", "b"], "case_sensitive": True, "split": None},
        Provenance.DECLARED,
        Status.UNVERIFIED,
    )
    obs = Fact.make(
        FactKind.VALUES, STATUS,
        {"observed": [{"value": "a", "count": 5}], "case_sensitive": True, "split": None},
        Provenance.MEASURED, Status.OBSERVED, method="exact", observed_at=_ago(1),
    )  # fmt: skip
    eff = resolve([exp, obs], now=NOW, ttl_days=14)[_key(FactKind.VALUES, STATUS)]
    assert eff.contradictions == ()


def test_catalog_type_beats_note_and_both_shown() -> None:
    col = ObjectRef(None, "shop", "orders", "created")
    cat = Fact.make(
        FactKind.TYPE_NOTE,
        col,
        {"stored_as": "STRING", "means": None},
        Provenance.CATALOG,
        Status.OBSERVED,
        observed_at=_ago(1),
    )
    note = Fact.make(
        FactKind.TYPE_NOTE,
        col,
        {"stored_as": "DATE", "means": "order date"},
        Provenance.HUMAN,
        Status.UNVERIFIED,
    )
    eff = resolve([note, cat], now=NOW, ttl_days=14)[_key(FactKind.TYPE_NOTE, col)]
    assert eff.primary is not None and eff.primary.id == cat.id
    assert {f.id for f in eff.all} == {cat.id, note.id}
    assert eff.contradictions == ("note says DATE; stored as STRING",)


def test_all_measured_retained_worst_warns() -> None:
    j1 = JoinRef(
        (ObjectRef(None, "shop", "orders", "customer_id"),),
        (ObjectRef(None, "shop", "customers", "id"),),
    )
    old_bad = Fact.make(
        FactKind.JOIN,
        j1,
        {"match_rate": 0.2},
        Provenance.MEASURED,
        Status.CONTRADICTED,
        method="exact",
        observed_at=_ago(3),
    )
    new_ok = Fact.make(
        FactKind.JOIN,
        j1,
        {"match_rate": 0.99, "verdict": "ok"},
        Provenance.MEASURED,
        Status.OBSERVED,
        method="exact",
        observed_at=_ago(1),
    )
    assert old_bad.id == new_ok.id
    eff = resolve([new_ok, old_bad], now=NOW, ttl_days=14)[("JOIN", j1.identity_key())]
    assert eff.observed is not None and eff.observed.observed_at == new_ok.observed_at
    assert eff.contradictions == ()  # the older contradiction was re-measured away
    # the other way round, the newest (worst) measurement warns, with its time and value
    new_bad = replace(old_bad, observed_at=_ago(0))
    eff = resolve([new_ok, new_bad], now=NOW, ttl_days=14)[("JOIN", j1.identity_key())]
    assert eff.contradictions == (
        f'measured contradiction at {_ago(0).isoformat()}: {{"match_rate":0.2}}',
    )
    # same id (identity ()): the re-measure supersedes; distinct ids are all kept.
    other = Fact.make(
        FactKind.NULL_RATE,
        STATUS,
        {"rate": 0.9},
        Provenance.MEASURED,
        Status.CONTRADICTED,
        method="exact",
        observed_at=_ago(5),
    )
    other2 = Fact.make(
        FactKind.NULL_RATE,
        STATUS,
        {"rate": 0.1},
        Provenance.MEASURED,
        Status.OBSERVED,
        method="exact",
        observed_at=_ago(1),
    )
    e2 = resolve([other, other2], now=NOW, ttl_days=14)[_key(FactKind.NULL_RATE, STATUS)]
    assert e2.observed is not None and e2.observed.value["rate"] == 0.1
    assert len(e2.all) == 1  # same id: newest re-measure replaces
    assert e2.contradictions == ()
    worst = resolve([replace(other, observed_at=_ago(0)), other2], now=NOW, ttl_days=14)
    assert worst[_key(FactKind.NULL_RATE, STATUS)].contradictions == (
        f'measured contradiction at {_ago(0).isoformat()}: {{"rate":0.9}}',
    )


def test_distinct_measured_all_retained_and_worst_warns() -> None:
    a = Fact.make(
        FactKind.PII,
        STATUS,
        {"category": "email"},
        Provenance.MEASURED,
        Status.CONTRADICTED,
        method="shape",
        observed_at=_ago(5),
    )
    b = Fact.make(
        FactKind.PII,
        STATUS,
        {"category": "phone"},
        Provenance.MEASURED,
        Status.OBSERVED,
        method="shape",
        observed_at=_ago(1),
    )
    eff = resolve([a, b], now=NOW, ttl_days=14)[_key(FactKind.PII, STATUS)]
    assert {f.id for f in eff.all} == {a.id, b.id}
    assert eff.observed is not None and eff.observed.id == b.id
    # a newer measurement of ANOTHER identity (phone) never clears the older warning (email)
    assert len(eff.contradictions) == 1
    older = next(f for f in eff.all if f.id == a.id)
    assert older.status is Status.CONTRADICTED
    # and the newest measurement of an identity warns when it is the contradicted one
    a2 = Fact.make(
        FactKind.PII, STATUS, {"category": "email"}, Provenance.MEASURED,
        Status.CONTRADICTED, method="shape", observed_at=_ago(0),
    )  # fmt: skip
    eff = resolve([a2, b], now=NOW, ttl_days=14)[_key(FactKind.PII, STATUS)]
    assert len(eff.contradictions) == 1


def test_draft_marked_unverified() -> None:
    d = Fact.make(
        FactKind.PITFALL,
        TBL,
        {"rule": "x"},
        Provenance.DRAFTED,
        Status.OBSERVED,
        observed_at=_ago(1),
    )
    eff = resolve([d], now=NOW, ttl_days=14)[_key(FactKind.PITFALL, TBL)]
    assert eff.primary is not None and eff.primary.status is Status.UNVERIFIED
    v = Fact.make(
        FactKind.PITFALL,
        TBL,
        {"rule": "y"},
        Provenance.DRAFTED,
        Status.VERIFIED,
        method="exact",
        observed_at=_ago(1),
    )
    eff = resolve([v], now=NOW, ttl_days=14)[_key(FactKind.PITFALL, TBL)]
    assert eff.primary is not None and eff.primary.status is Status.VERIFIED


def test_stale_after_ttl() -> None:
    old = Fact.make(
        FactKind.ROW_COUNT,
        TBL,
        {"rows": 3},
        Provenance.MEASURED,
        Status.OBSERVED,
        method="exact",
        observed_at=_ago(15),
    )
    fresh = Fact.make(
        FactKind.NULL_RATE,
        STATUS,
        {"rate": 0.0},
        Provenance.MEASURED,
        Status.OBSERVED,
        method="exact",
        observed_at=_ago(13),
    )
    res = resolve([old, fresh], now=NOW, ttl_days=14)
    r = res[_key(FactKind.ROW_COUNT, TBL)].primary
    assert r is not None and r.status is Status.STALE and r.id == old.id
    f = res[_key(FactKind.NULL_RATE, STATUS)].primary
    assert f is not None and f.status is Status.OBSERVED


def _contradicted() -> Fact:
    return Fact.make(
        FactKind.NULL_RATE,
        STATUS,
        {"rate": 0.5},
        Provenance.MEASURED,
        Status.CONTRADICTED,
        method="exact",
        observed_at=_ago(1),
    )


def test_waiver_applies_and_expired_ignored() -> None:
    f = _contradicted()
    live = Waiver("NULL_RATE shop.orders.status", "known", date(2026, 12, 1))
    eff = resolve([f], now=NOW, ttl_days=14, waivers=[live])[_key(FactKind.NULL_RATE, STATUS)]
    assert eff.primary is not None and eff.primary.status is Status.WAIVED
    assert eff.contradictions and eff.contradictions[0].startswith(
        "WAIVED until 2026-12-01 (known): "
    )


def test_waiver_expired_ignored() -> None:
    f = _contradicted()
    dead = Waiver("NULL_RATE shop.orders.*", "known", date(2026, 11, 23))
    eff = resolve([f], now=NOW, ttl_days=14, waivers=[dead])[_key(FactKind.NULL_RATE, STATUS)]
    assert eff.primary is not None and eff.primary.status is Status.CONTRADICTED
    wrong = Waiver("ROW_COUNT shop.orders.status", "k", date(2027, 1, 1))
    eff = resolve([f], now=NOW, ttl_days=14, waivers=[wrong])[_key(FactKind.NULL_RATE, STATUS)]
    assert eff.primary is not None and eff.primary.status is Status.CONTRADICTED


def test_waiver_validation() -> None:
    with pytest.raises(ValueError):
        Waiver("VALUES", "r", date(2027, 1, 1))
    assert Waiver("VALUES shop.*", "r", date(2027, 1, 1)).matches("VALUES", "shop.orders.status")
    assert not Waiver("VALUES shop.orders.status", "r", date(2027, 1, 1)).matches(
        "VALUES", "shop.orders.Status"
    )


def test_estimated_never_confirms() -> None:
    exp = Fact.make(
        FactKind.VALUES,
        STATUS,
        {"values": ["a"], "case_sensitive": True, "split": None},
        Provenance.HUMAN,
        Status.UNVERIFIED,
    )
    est = Fact.make(
        FactKind.VALUES, STATUS,
        {"observed": [{"value": "a", "count": 1}], "case_sensitive": True, "split": None},
        Provenance.MEASURED, Status.ESTIMATED, method="sampled:1%", observed_at=_ago(1),
    )  # fmt: skip
    eff = resolve([exp, est], now=NOW, ttl_days=14)[_key(FactKind.VALUES, STATUS)]
    assert eff.expectation is not None and eff.expectation.status is Status.UNVERIFIED
    assert eff.contradictions == ()


def test_primary_key_expectation_not_replaced_by_catalog() -> None:
    t = TBL
    cat = Fact.make(
        FactKind.PRIMARY_KEY,
        t,
        {"columns": ["id"]},
        Provenance.CATALOG,
        Status.OBSERVED,
        observed_at=_ago(1),
    )
    hum = Fact.make(
        FactKind.PRIMARY_KEY, t, {"columns": ["id", "tenant"]}, Provenance.HUMAN, Status.UNVERIFIED
    )
    eff = resolve([cat, hum], now=NOW, ttl_days=14)[_key(FactKind.PRIMARY_KEY, t)]
    assert eff.primary is not None and eff.primary.id == hum.id
    assert eff.expectation is not None and eff.expectation.id == hum.id
    assert len(eff.contradictions) == 1 and "catalog primary key" in eff.contradictions[0]


def test_declared_expectation_checked_alongside_human() -> None:
    human = Fact.make(
        FactKind.VALUES,
        STATUS,
        {"values": ["a", "b", "c"], "case_sensitive": True, "split": None},
        Provenance.HUMAN,
        Status.UNVERIFIED,
    )
    decl = Fact.make(
        FactKind.VALUES,
        STATUS,
        {"values": ["a", "b"], "case_sensitive": True, "split": None},
        Provenance.DECLARED,
        Status.UNVERIFIED,
    )
    obs = Fact.make(
        FactKind.VALUES, STATUS,
        {
            "case_sensitive": True, "split": None,
            "observed": [{"value": "a", "count": 1}, {"value": "c", "count": 9}],
        },
        Provenance.MEASURED, Status.OBSERVED, method="exact", observed_at=_ago(1),
    )  # fmt: skip
    eff = resolve([obs, decl, human], now=NOW, ttl_days=14)[_key(FactKind.VALUES, STATUS)]
    assert eff.expectation is not None and eff.expectation.id == human.id
    assert len(eff.contradictions) == 1  # human covers c; declared does not
    assert "declared values a, b" in eff.contradictions[0] and "c (9 rows)" in eff.contradictions[0]


def test_pii_human_beats_measured() -> None:
    m = Fact.make(
        FactKind.PII,
        STATUS,
        {"category": "email"},
        Provenance.MEASURED,
        Status.OBSERVED,
        method="shape",
        observed_at=_ago(1),
    )
    h = Fact.make(FactKind.PII, STATUS, {"category": "other"}, Provenance.HUMAN, Status.UNVERIFIED)
    eff = resolve([m, h], now=NOW, ttl_days=14)[_key(FactKind.PII, STATUS)]
    assert eff.primary is not None and eff.primary.id == h.id
    assert eff.observed is not None and eff.observed.id == m.id


def _nr(cat: str, status: Status, days: float, method: str = "exact") -> Fact:
    # PII identity is the category, so distinct categories are distinct measured facts
    return Fact.make(
        FactKind.PII, STATUS, {"category": cat}, Provenance.MEASURED, status,
        method=method, observed_at=_ago(days),
    )  # fmt: skip


def _eff_nr(*fs: Fact):  # type: ignore[no-untyped-def]
    return resolve(list(fs), now=NOW, ttl_days=14)[_key(FactKind.PII, STATUS)]


def test_newer_sample_does_not_clear_exact_contradiction() -> None:
    bad = _nr("email", Status.CONTRADICTED, 3)
    ok_sample = _nr("email", Status.ESTIMATED, 1, method="sampled:1%")
    eff = _eff_nr(bad, ok_sample)
    assert len(eff.contradictions) == 1
    assert next(f for f in eff.all if f.id == bad.id).status is Status.CONTRADICTED


def test_newer_sample_contradiction_shown_marked_sampled() -> None:
    ok = _nr("email", Status.OBSERVED, 3)
    bad_sample = _nr("email", Status.CONTRADICTED, 1, method="sampled:1%")
    eff = _eff_nr(ok, bad_sample)
    assert len(eff.contradictions) == 1 and "[sampled]" in eff.contradictions[0]


def test_newer_exact_ok_supersedes_older_contradiction() -> None:
    bad = _nr("email", Status.CONTRADICTED, 3, method="sampled:1%")
    ok = _nr("email", Status.OBSERVED, 1)
    eff = _eff_nr(bad, ok)
    assert eff.contradictions == ()
    assert next(f for f in eff.all if f.id == bad.id).status is Status.STALE


def test_older_sample_contradiction_not_cleared_by_newer_sample() -> None:
    bad = _nr("email", Status.CONTRADICTED, 3)
    ok = _nr("email", Status.ESTIMATED, 1, method="sampled:2%")
    assert len(_eff_nr(bad, ok).contradictions) == 1


def test_values_union_of_exact_and_newer_sample() -> None:
    exp = Fact.make(
        FactKind.VALUES,
        STATUS,
        {"values": ["a"], "case_sensitive": True, "split": None},
        Provenance.HUMAN,
        Status.UNVERIFIED,
    )
    exact = Fact.make(
        FactKind.VALUES, STATUS,
        {"case_sensitive": True, "split": None, "observed": [
            {"value": "a", "count": 1}, {"value": "PAID", "count": 7}
        ]},
        Provenance.MEASURED, Status.OBSERVED, method="exact", observed_at=_ago(3),
    )  # fmt: skip
    sample = Fact.make(
        FactKind.VALUES, STATUS,
        {"case_sensitive": True, "split": ";", "observed": [
            {"value": "a", "count": 1}, {"value": "z", "count": 2}
        ]},
        Provenance.MEASURED, Status.ESTIMATED, method="sampled:1%", observed_at=_ago(1),
    )  # fmt: skip
    eff = resolve([exp, exact, sample], now=NOW, ttl_days=14)[_key(FactKind.VALUES, STATUS)]
    assert len(eff.contradictions) == 1
    msg = eff.contradictions[0]
    assert "PAID (7 rows)" in msg and "z (2 rows) [sampled]" in msg
    assert "PAID (7 rows) [sampled]" not in msg


def test_newest_error_is_a_warning_never_the_pivot() -> None:
    col = ObjectRef(None, "shop", "orders", "deleted")
    good = Fact.make(
        FactKind.NULL_RATE, col, {"rate": 0.97}, Provenance.MEASURED, Status.OBSERVED,
        method="exact", observed_at=_ago(2),
    )  # fmt: skip
    older = Fact.make(
        FactKind.NULL_RATE, col, {"rate": 0.5}, Provenance.MEASURED, Status.ESTIMATED,
        method="sampled:system:0.01:1", observed_at=_ago(5),
    )  # fmt: skip
    err = Fact.make(
        FactKind.NULL_RATE, col, {"rate": None, "reason": "over_budget", "code": "SG301"},
        Provenance.MEASURED, Status.ERROR, method="exact", observed_at=_ago(1),
    )  # fmt: skip
    eff = resolve([good, older, err], now=NOW, ttl_days=14)[_key(FactKind.NULL_RATE, col)]
    assert eff.primary is not None and eff.primary.id == good.id
    assert eff.observed is not None and eff.observed.id == good.id
    assert any("over_budget" in m for m in eff.contradictions)
    by_id = {f.id: f for f in eff.all}
    assert by_id[good.id].status is Status.OBSERVED  # the error superseded nothing
    # the older sample is superseded by the newer exact measurement, like an exact one would be
    assert by_id[older.id].status is Status.STALE
    # an error older than the good measurement no longer warns
    old_err = Fact.make(
        FactKind.NULL_RATE, col, {"rate": None, "reason": "probe_error"}, Provenance.MEASURED,
        Status.ERROR, method="exact", observed_at=_ago(3),
    )  # fmt: skip
    eff = resolve([good, old_err], now=NOW, ttl_days=14)[_key(FactKind.NULL_RATE, col)]
    assert eff.primary.id == good.id and eff.contradictions == ()  # type: ignore[union-attr]
    # with only an error, it is all there is
    eff = resolve([err], now=NOW, ttl_days=14)[_key(FactKind.NULL_RATE, col)]
    assert eff.primary is not None and eff.primary.status is Status.ERROR


ORDERS_T = ObjectRef(None, "shop", "orders", None)


def _pk(cols: list[str], status: Status, days: float, method: str = "exact") -> Fact:
    return Fact.make(
        FactKind.PRIMARY_KEY, ORDERS_T, {"columns": cols}, Provenance.MEASURED, status,
        method=method, observed_at=_ago(days),
    )  # fmt: skip


def test_newer_exact_of_other_identity_does_not_supersede() -> None:
    bad = _pk(["order_id"], Status.CONTRADICTED, 3)
    other = _pk(["email"], Status.OBSERVED, 1)
    eff = resolve([bad, other], now=NOW, ttl_days=14)[_key(FactKind.PRIMARY_KEY, ORDERS_T)]
    assert len(eff.contradictions) == 1
    assert next(f for f in eff.all if f.id == bad.id).status is Status.CONTRADICTED


def test_same_identity_remeasure_still_supersedes_pk() -> None:
    bad = _pk(["order_id"], Status.CONTRADICTED, 3, method="sampled:1%")
    ok = _pk(["order_id"], Status.OBSERVED, 1)
    eff = resolve([bad, ok], now=NOW, ttl_days=14)[_key(FactKind.PRIMARY_KEY, ORDERS_T)]
    assert eff.contradictions == ()
    assert next(f for f in eff.all if f.id == bad.id).status is Status.STALE


def test_values_with_different_split_do_not_supersede() -> None:
    def v(split: str | None, st: Status, days: float) -> Fact:
        return Fact.make(
            FactKind.VALUES, STATUS,
            {"case_sensitive": True, "split": split, "observed": []},
            Provenance.MEASURED, st, method="exact", observed_at=_ago(days),
        )  # fmt: skip

    bad, ok = v(None, Status.CONTRADICTED, 3), v(";", Status.OBSERVED, 1)
    eff = resolve([bad, ok], now=NOW, ttl_days=14)[_key(FactKind.VALUES, STATUS)]
    assert next(f for f in eff.all if f.id == bad.id).status is Status.CONTRADICTED
    assert len(eff.contradictions) == 1


def test_estimated_goes_stale(tmp_path: Path) -> None:
    def est(days: float) -> Fact:
        return Fact.make(
            FactKind.NULL_RATE, STATUS, {"rate": 0.25}, Provenance.MEASURED, Status.ESTIMATED,
            method="sampled:system:0.01:7", observed_at=_ago(days),
        )  # fmt: skip

    fresh = resolve([est(3)], now=NOW, ttl_days=14)[_key(FactKind.NULL_RATE, STATUS)]
    assert fresh.primary is not None and fresh.primary.status is Status.ESTIMATED
    old = resolve([est(30)], now=NOW, ttl_days=14)[_key(FactKind.NULL_RATE, STATUS)]
    assert old.primary is not None and old.primary.status is Status.STALE
    # a stale estimate still renders as an estimate, never as an exact-looking number
    from schemaguru.model.detail import DetailLevel
    from schemaguru.render import collect, get_profile
    from tests.golden.shop_knowledge import CONFIG, shop_store, t

    later = datetime(2026, 12, 24, 12, tzinfo=timezone.utc)  # 34 days after the measurements
    with shop_store(tmp_path / "shop.db") as s:
        text = get_profile("agent").render_table(
            collect(s, t("events"), later, CONFIG), DetailLevel.FULL
        )
    assert re.search(r"null ≈ 0% \(sampled 1%", text), text
    assert "distinct ≥ 190 (sampled 1%, lower bound)" in text


def test_superseded_estimate_stale() -> None:
    sample = _nr("email", Status.ESTIMATED, 3, method="sampled:1%")
    exact = _nr("email", Status.OBSERVED, 1)
    eff = _eff_nr(sample, exact)
    assert next(f for f in eff.all if f.id == sample.id).status is Status.STALE
    assert eff.observed is not None and eff.observed.id == exact.id
    # a sample newer than the exact pivot is not superseded and stays an estimate
    newer = _nr("email", Status.ESTIMATED, 0, method="sampled:1%")
    eff = _eff_nr(newer, exact)
    assert next(f for f in eff.all if f.id == newer.id).status is Status.ESTIMATED


def test_sampled_extra_seen_exactly() -> None:
    exp = Fact.make(
        FactKind.VALUES, STATUS, {"values": ["a"], "case_sensitive": True, "split": None},
        Provenance.HUMAN, Status.UNVERIFIED,
    )  # fmt: skip

    def obs(status: Status, method: str, days: float, items: list[tuple[str, int]]) -> Fact:
        return Fact.make(
            FactKind.VALUES, STATUS,
            {"case_sensitive": True, "split": None,
             "observed": [{"value": v, "count": n} for v, n in items]},
            Provenance.MEASURED, status, method=method, observed_at=_ago(days),
        )  # fmt: skip

    exact = obs(Status.OBSERVED, "exact", 3, [("a", 1), ("z", 4)])
    sample = obs(Status.ESTIMATED, "sampled:1%", 1, [("z", 2), ("q", 1)])
    for facts in ([exp, exact, sample], [sample, exact, exp]):
        eff = resolve(facts, now=NOW, ttl_days=14)[_key(FactKind.VALUES, STATUS)]
        (msg,) = eff.contradictions
        # z: seen by the sample AND by the exact measurement -> not marked [sampled]
        assert msg == "declared values a - observed also: z (4 rows), q (1 rows) [sampled]"
