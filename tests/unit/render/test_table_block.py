"""Agent table blocks: spec 13.3 rules, one by one."""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from schemaguru.model.detail import DetailLevel
from schemaguru.model.facts import Fact, Provenance, Status
from schemaguru.model.kinds import FactKind
from schemaguru.model.refs import ObjectRef
from schemaguru.model.structure import Column, Constraint, Table
from schemaguru.render import collect, get_profile
from schemaguru.render.table_block import estimate_text, pct, sig4
from schemaguru.store.sqlite import SQLiteStore
from tests.golden.golden import assert_golden
from tests.golden.shop_knowledge import (
    CONFIG,
    DRAFTED_AT,
    NOW,
    c,
    human,
    j,
    measured,
    shop_facts,
    shop_store,
    t,
)

AGENT = get_profile("agent")
CONFIRMATION = re.compile(r"\b(measured|verified|declared)\b|\bmatch \d")


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SQLiteStore]:
    with shop_store(tmp_path / "shop.db") as s:
        yield s


def block(store: SQLiteStore, name: str, level: DetailLevel = DetailLevel.FULL) -> str:
    return AGENT.render_table(collect(store, t(name), NOW, CONFIG), level)


def test_table_block_matches_spec_example(store: SQLiteStore) -> None:
    text = block(store, "orders")
    lines = text.splitlines()
    assert lines[0] == "[shop.orders] One row per order. (draft)"
    # every element of the spec 13.3 example, with the shop fixture's real numbers
    assert "Answers (draft): revenue; order volume; basket size" in text
    assert "order_id" in text and "PK (unique, measured 2026-11-20)" in text
    assert "→ shop.customers.customer_id N:1, match 98.04%" in text
    assert "values: 'paid','refunded','cancelled'" in text
    assert "'PAID' (412 rows)" in text and "case" in text
    assert "stored as VARCHAR, holds ISO-8601 timestamp" in text and "CAST" in text
    assert "live rows: (deleted IS NULL OR NOT deleted)" in text
    assert "never FALSE" in text
    assert "⚠ Pitfall (critical): a refund is a second payment row" in text
    assert lines[-1].startswith("  Provenance: ")
    assert_golden("orders_agent_full.txt", text)


def test_warning_lines_first(store: SQLiteStore) -> None:
    seen_warning = False
    for name in ("orders", "customers", "events", "payments", "broken_child", "products"):
        for level in DetailLevel:
            lines = block(store, name, level).splitlines()
            first_confirmation = next(
                (i for i, ln in enumerate(lines) if "⚠" not in ln and CONFIRMATION.search(ln)),
                len(lines),
            )
            warn = [i for i, ln in enumerate(lines) if "⚠" in ln]
            seen_warning = seen_warning or bool(warn)
            assert all(i < first_confirmation for i in warn), (name, level, lines)
    assert seen_warning  # negative control: the shop has hazards to order


def test_render_estimated_marker(store: SQLiteStore) -> None:
    text = block(store, "events")
    assert re.search(r"null ≈ 0% \(sampled 1%, 0% to 3\.713%, approximate\)", text), text
    assert "distinct ≥ 190 (sampled 1%, lower bound)" in text
    assert "seen in sample" in text
    # a sampled number is never called verified or plainly measured
    for line in text.splitlines():
        if "≈" in line or "≥" in line:
            assert "verified" not in line


def test_estimate_text_matches_spec_wording() -> None:
    est = {"value": 0.032, "low": 0.028, "high": 0.036, "lower_bound": False}
    assert estimate_text(est, "0.01") == "≈ 3.2% (sampled 1%, ±0.4%)"
    est_b = {**est, "approximate": True}
    assert estimate_text(est_b, "0.01") == "≈ 3.2% (sampled 1%, ±0.4%, approximate)"
    lb = {"value": 1234, "low": 1234, "high": None, "lower_bound": True}
    assert estimate_text(lb, "0.001", rate=False) == "≥ 1,234 (sampled 0.1%, lower bound)"
    scaled = {"value": 1234567, "low": None, "high": None, "lower_bound": False}
    assert estimate_text(scaled, "0.5", rate=False) == "≈ 1,235,000 (sampled 50%)"
    assert estimate_text({"value": None}, None) == "unknown"


def test_numbers_four_significant_digits() -> None:
    assert sig4(0.98039215) == "0.9804"
    assert sig4(1234567) == "1,235,000"
    assert sig4(12.0) == "12"
    assert sig4(0.000012345) == "0.00001234" or sig4(0.000012345) == "0.00001235"
    assert pct(1.0) == "100%"
    assert pct(0.998) == "99.8%"
    assert pct(0.05) == "5%"
    assert pct(0.003) == "0.3%"


def test_render_quotes_odd_identifiers(tmp_path: Path) -> None:
    ref = ObjectRef(None, "shop", "Order Items")
    other = ObjectRef(None, "shop", "orders")
    with SQLiteStore(tmp_path / "odd.db") as s:
        s.upsert_tables([Table(ref, "table"), Table(other, "table")])
        s.upsert_columns(
            ref,
            [
                Column(ObjectRef(None, "shop", "Order Items", "Line No"), 1, "INTEGER", "integer"),
                Column(ObjectRef(None, "shop", "Order Items", "order_id"), 2, "INTEGER", "integer"),
            ],
        )
        s.upsert_columns(other, [Column(c("orders", "order_id"), 1, "INTEGER", "integer")])
        s.upsert_constraints(
            ref,
            [
                Constraint("primary_key", ref, ("Line No",)),
                Constraint("foreign_key", ref, ("order_id",), other, ("order_id",)),
            ],
        )
        k = collect(s, ref, NOW, CONFIG)
        text = AGENT.render_table(k, DetailLevel.FULL)
        assert text.splitlines()[0].startswith('[shop."Order Items"]')
        assert '"Line No"' in text
        assert "→ shop.orders.order_id (declared FK, not measured)" in text
        for level in DetailLevel:
            assert 'shop."Order Items"' in AGENT.render_table(k, level)
            for name in ("planner", "verifier", "router"):
                assert 'shop."Order Items"' in get_profile(name).render_table(k, level)


def test_values_capped_with_more(tmp_path: Path) -> None:
    many = [f"v{i}" for i in range(9)]
    extra = human(
        FactKind.VALUES,
        c("customers", "segment"),
        {"values": many, "case_sensitive": True, "split": None},
    )
    with shop_store(tmp_path / "s.db", [*shop_facts(), extra]) as s:
        text = AGENT.render_table(collect(s, t("customers"), NOW, CONFIG), DetailLevel.FULL)
    assert "values: 'v0','v1','v2','v3','v4','v5' +3 more" in text
    # the measured values the declaration lacks are a contradiction, shown as a warning
    assert "⚠ segment: declared values" in text


def test_case_drift_line_for_measured_only_values(tmp_path: Path) -> None:
    f = measured(
        FactKind.VALUES,
        c("customers", "segment"),
        {
            "case_sensitive": True,
            "split": None,
            "distinct": 2,
            "non_null": 10,
            "observed": [{"value": "vip", "count": 8}, {"value": "VIP", "count": 2}],
        },
    )
    facts = [x for x in shop_facts() if x.subject != c("customers", "segment")] + [f]
    with shop_store(tmp_path / "s.db", facts) as s:
        text = AGENT.render_table(collect(s, t("customers"), NOW, CONFIG), DetailLevel.FULL)
    assert "⚠ segment: case drift: 'vip'/'VIP' differ only in case" in text


def test_drafted_facts_marked_whatever_their_status(tmp_path: Path) -> None:
    failed = Fact.make(
        FactKind.DESCRIPTION,
        t("products"),
        {"text": None, "reason": "invalid reply"},
        Provenance.DRAFTED,
        Status.ERROR,
        source="llm:test-model",
        observed_at=DRAFTED_AT,
    )
    empty = Fact.make(
        FactKind.USE_CASES,
        t("products"),
        {"questions": ["catalogue size"]},
        Provenance.DRAFTED,
        Status.UNMEASURABLE,
        source="llm:test-model",
        observed_at=DRAFTED_AT,
    )
    facts = [x for x in shop_facts() if x.subject != t("products") or x.kind.name != "DESCRIPTION"]
    with shop_store(tmp_path / "s.db", [*facts, failed, empty]) as s:
        text = AGENT.render_table(collect(s, t("products"), NOW, CONFIG), DetailLevel.FULL)
    assert "⚠ description draft failed: invalid reply (draft)" in text
    assert "Answers (draft): catalogue size" in text


def test_observed_says_measured_never_verified(store: SQLiteStore) -> None:
    text = block(store, "customers")
    assert "PK (unique, measured 2026-11-20)" in text
    assert "verified" not in text  # nothing about customers is a passed claim
    # a verified drafted soft-delete says verified, without the draft marker
    orders = block(store, "orders")
    line = next(ln for ln in orders.splitlines() if "live rows" in ln)
    assert "verified 2026-11-19" in line and "(draft)" not in line


def test_thin_join_states_coverage(store: SQLiteStore) -> None:
    text = block(store, "payments")
    assert "→ shop.orders.order_id 1:1, match 100%, coverage 5%" in text


def test_broken_join_warns(store: SQLiteStore) -> None:
    text = block(store, "events")
    assert "⚠ session_id → shop.orders.order_id BROKEN: match 0% (measured 2026-11-20)" in text
    assert "do not join" in text


def test_never_query_first_at_every_level(store: SQLiteStore) -> None:
    for level in DetailLevel:
        lines = block(store, "broken_child", level).splitlines()
        assert "NEVER QUERY" in lines[0] or "NEVER QUERY" in lines[1], (level, lines)


def test_stale_and_unmeasurable(store: SQLiteStore) -> None:
    assert "200 rows (measured 2026-10-01, stale)" in block(store, "products")
    assert "rows: unmeasurable (empty table)" in block(store, "empty_t")
    assert "⚠ c: always NULL (dead column, measured 2026-11-20)" in block(store, "all_null_t")


def test_unmeasured_table_is_hedged_above_its_columns(tmp_path: Path) -> None:
    with shop_store(tmp_path / "s.db", []) as s:
        lines = AGENT.render_table(collect(s, t("orders"), NOW, CONFIG), DetailLevel.FULL)
    text = lines.splitlines()
    hedge = next(i for i, ln in enumerate(text) if "unmeasured" in ln)
    first_col = next(i for i, ln in enumerate(text) if ln.startswith("    order_id"))
    assert hedge < first_col


def test_levels_shrink_and_lite_keeps_soft_delete(store: SQLiteStore) -> None:
    for name in ("orders", "customers", "events"):
        sizes = [len(block(store, name, lv)) for lv in DetailLevel]
        assert sizes == sorted(sizes, reverse=True), (name, sizes)
    lite = block(store, "orders", DetailLevel.LITE)
    assert "    deleted" in lite
    name_only = block(store, "orders", DetailLevel.NAME_ONLY)
    assert name_only.startswith("[shop.orders]") and len(name_only.splitlines()) == 1


def test_lite_reports_hidden_columns(tmp_path: Path) -> None:
    ref = t("wide")
    cols = [Column(c("wide", f"c{i:02d}"), i, "INTEGER", "integer") for i in range(1, 21)]
    with SQLiteStore(tmp_path / "w.db") as s:
        s.upsert_tables([Table(ref, "table")])
        s.upsert_columns(ref, cols)
        text = AGENT.render_table(collect(s, ref, NOW, CONFIG), DetailLevel.LITE)
    assert "+12 more columns" in text


def test_declared_key_check_preferred_over_other_candidates(tmp_path: Path) -> None:
    from datetime import timedelta

    newer = Fact.make(
        FactKind.PRIMARY_KEY,
        t("customers"),
        {"columns": ["email"], "unique": False, "duplicate_keys": 3},
        Provenance.MEASURED,
        Status.OBSERVED,
        method="exact",
        observed_at=NOW - timedelta(days=1),
    )
    with shop_store(tmp_path / "s.db", [*shop_facts(), newer]) as s:
        text = AGENT.render_table(collect(s, t("customers"), NOW, CONFIG), DetailLevel.FULL)
    # the declared key (customer_id) keeps its own result; the email candidate is not the PK.
    # (knowledge.resolve groups PRIMARY_KEY facts by table, so the newer email check marks the
    # older customer_id check STALE; the renderer reports that status as it is.)
    assert "    customer_id  INTEGER  PK (unique, measured 2026-11-20" in text
    assert "not unique" not in text


def _m(kind: Any, subject: Any, value: dict[str, Any], **kw: Any) -> Fact:
    return Fact.make(
        kind,
        subject,
        value,
        kw.pop("provenance", Provenance.MEASURED),
        kw.pop("status", Status.OBSERVED),
        method=kw.pop("method", "exact"),
        observed_at=kw.pop("at", MEASURED),
        **kw,
    )


MEASURED = datetime(2026, 11, 20, 9, 30, tzinfo=timezone.utc)


def test_hazard_paths_render_truthfully(tmp_path: Path) -> None:
    from schemaguru.model.joins import JOIN_VALUE_KEYS

    F = FactKind
    jv = {k: None for k in JOIN_VALUE_KEYS}
    sampled_join = {
        **jv,
        "match_rate": 0.97,
        "coverage": 0.5,
        "cardinality": "N:1",
        "fraction": "0.1",
        "estimates": {
            "match_rate": {"value": 0.97, "low": 0.96, "high": 0.98, "lower_bound": False},
            "coverage": {"value": 0.5, "low": 0.45, "high": 0.55, "lower_bound": False},
        },
    }
    extra = [
        # products: a weak join, a cardinality contradiction, a failed probe, a sampled join
        _m(
            F.JOIN,
            j("products.product_id", "ref_parent.id"),
            {**jv, "match_rate": 0.5, "coverage": 0.1, "cardinality": "N:M"},
        ),
        human(F.JOIN, j("products.product_id", "ref_parent.id"), {"cardinality": "N:1"}),
        _m(
            F.JOIN,
            j("products.name", "accounts.name"),
            sampled_join,
            status=Status.ESTIMATED,
            method="sampled:keyhash:0.1:7",
        ),
        _m(
            F.NULL_RATE,
            c("products", "name"),
            {"rate": None, "reason": "probe_error"},
            status=Status.ERROR,
        ),
        # sessions: freshness, a non-unique key, a key with NULL parts, a contradicted soft delete
        _m(
            F.FRESHNESS,
            t("sessions"),
            {"column": "started_at", "max_value": "2026-11-19", "age_seconds": 129600.0},
        ),
        _m(
            F.PRIMARY_KEY,
            t("sessions"),
            {"columns": ["session_id"], "unique": False, "duplicate_keys": 7, "complete": False},
        ),
        Fact.make(
            F.SOFT_DELETE,
            t("sessions"),
            {"predicate": "started_at IS NOT NULL", "marker_columns": None},
            Provenance.DRAFTED,
            Status.CONTRADICTED,
            observed_at=MEASURED,
        ),
        Fact.make(
            F.TYPE_NOTE,
            c("sessions", "started_at"),
            {"stored_as": "DATE", "means": "a day"},
            Provenance.DRAFTED,
            Status.UNVERIFIED,
        ),
        _m(
            F.TYPE_NOTE,
            c("sessions", "session_id"),
            {"stored_as": "string", "means": "UUID", "rate": 0.4, "holds": False},
        ),
        # accounts: a human soft delete without markers, a drafted join candidate
        human(F.SOFT_DELETE, t("accounts"), {"predicate": "name <> ''", "marker_columns": None}),
        Fact.make(
            F.JOIN,
            j("accounts.name", "customers.email"),
            {**jv, "candidate": True},
            Provenance.DRAFTED,
            Status.UNVERIFIED,
        ),
    ]
    with shop_store(tmp_path / "s.db", [*shop_facts(), *extra]) as s:
        products = AGENT.render_table(collect(s, t("products"), NOW, CONFIG), DetailLevel.FULL)
        sessions = AGENT.render_table(collect(s, t("sessions"), NOW, CONFIG), DetailLevel.FULL)
        accounts = AGENT.render_table(collect(s, t("accounts"), NOW, CONFIG), DetailLevel.FULL)
        verifier = get_profile("verifier").render_table(
            collect(s, t("sessions"), NOW, CONFIG), DetailLevel.FULL
        )
    assert "⚠ product_id → shop.ref_parent.id WEAK: match 50% (measured 2026-11-20)" in products
    assert "declared N:1 but measured N:M" in products
    assert "⚠ name null rate: measurement failed (probe error, 2026-11-20)" in products
    assert "→ shop.accounts.name N:1, match ≈ 97% (sampled 10%, ±1%), coverage ≈ 50%" in products
    assert "⚠ PK (session_id) is not unique: 7 duplicate keys" in sessions
    assert "has rows with a NULL key part" in sessions
    assert "⚠ live-rows filter (started_at IS NOT NULL) failed its check" in sessions
    assert "⚠ started_at: stored as DATE, holds a day (draft)" in sessions
    assert "newest started_at: 2026-11-19, age 1.5 days (measured 2026-11-20)" in sessions
    assert "40% of values look like UUID" in sessions
    assert "  live rows: (name <> '') (human, unchecked)" in accounts
    assert "→ shop.customers.email (draft candidate, not measured)" in accounts
    assert "FRESH started_at max=2026-11-19 measured 2026-11-20" in verifier
    assert "PK session_id NOT unique see warning" in verifier


def test_pk_consistent_with_sample_is_not_called_unique(tmp_path: Path) -> None:
    f = _m(
        FactKind.PRIMARY_KEY,
        t("events"),
        {
            "columns": ["event_id"],
            "unique": None,
            "consistent_with_sample": True,
            "fraction": "0.01",
        },
        status=Status.ESTIMATED,
        method="sampled:keyhash:0.01:3",
    )
    with shop_store(tmp_path / "s.db", [*shop_facts(), f]) as s:
        text = AGENT.render_table(collect(s, t("events"), NOW, CONFIG), DetailLevel.FULL)
    assert "PK (no duplicate in sample (sampled 1%))" in text
    assert "unique," not in text


def test_waived_contradiction_keeps_its_warning_and_says_waived(store: SQLiteStore) -> None:
    from schemaguru.model.facts import Waiver

    w = Waiver("VALUES shop.orders.status", "case fixed upstream next sprint", date(2026, 12, 31))
    k = collect(store, t("orders"), NOW, CONFIG, waivers=[w])
    text = AGENT.render_table(k, DetailLevel.FULL)
    assert (
        "⚠ WAIVED until 2026-12-31 (case fixed upstream next sprint): status: declared values"
        in text
    )


def test_rounding_never_overstates_certainty() -> None:
    assert pct(0.99996) == ">99.99%"
    assert pct(1.0) == "100%"
    assert pct(0.0) == "0%"
    assert pct(1e-7) == "<0.01%" or pct(1e-7).startswith("0.00001")
    assert sig4(1239.9, floor=True) == "1,239"  # a lower bound is never rounded up
    assert sig4(12_345_678, floor=True) == "12,345,678"
    lb = {"value": 1239.9, "low": 1239.9, "high": None, "lower_bound": True}
    assert estimate_text(lb, "0.01", rate=False) == "≥ 1,239 (sampled 1%, lower bound)"
    # an interval squeezed against 0 is shown as its range, not as a symmetric ±
    near0 = {"value": 0.0, "low": 0.0, "high": 0.02, "lower_bound": False}
    assert estimate_text(near0, "0.01") == "≈ 0% (sampled 1%, 0% to 2%)"


def test_sampled_join_without_interval_is_still_an_estimate(tmp_path: Path) -> None:
    from schemaguru.model.joins import JOIN_VALUE_KEYS

    v = {k: None for k in JOIN_VALUE_KEYS}
    v.update(match_rate=0.97, coverage=0.5, cardinality=None, fraction="0.1")
    f = _m(
        FactKind.JOIN,
        j("products.name", "accounts.name"),
        v,
        status=Status.ESTIMATED,
        method="sampled:keyhash:0.1:7",
    )
    with shop_store(tmp_path / "s.db", [*shop_facts(), f]) as s:
        k = collect(s, t("products"), NOW, CONFIG)
        agent = AGENT.render_table(k, DetailLevel.FULL)
        planner = get_profile("planner").render_table(k, DetailLevel.FULL)
        verifier = get_profile("verifier").render_table(k, DetailLevel.FULL)
        accounts = AGENT.render_table(collect(s, t("accounts"), NOW, CONFIG), DetailLevel.FULL)
    assert "→ shop.accounts.name, match ≈ 97% (sampled 10%), coverage ≈ 50% (sampled 10%)" in agent
    assert "name → shop.accounts.name (match ≈ 97% (sampled 10%))" in planner
    assert "(None" not in planner
    assert "match=≈0.97 coverage=≈0.5 sampled" in verifier
    assert "← shop.products.name (coverage ≈ 50% (sampled 10%))" in accounts


def test_estimated_type_note_is_not_an_exact_rate(tmp_path: Path) -> None:
    f = _m(
        FactKind.TYPE_NOTE,
        c("events", "kind"),
        {
            "stored_as": "string",
            "means": "a code",
            "rate": 0.995,
            "holds": True,
            "fraction": "0.01",
            "estimate": {"value": 0.995, "low": 0.99, "high": 1.0, "lower_bound": False},
        },
        status=Status.ESTIMATED,
        method="sampled:system:0.01:1",
    )
    with shop_store(tmp_path / "s.db", [*shop_facts(), f]) as s:
        text = AGENT.render_table(collect(s, t("events"), NOW, CONFIG), DetailLevel.FULL)
    assert "looks like a code: ≈ 99.5% (sampled 1%, 99% to 100%) of values" in text
    assert "(99.5% of values" not in text


def test_type_note_wording_follows_the_column_type(tmp_path: Path) -> None:
    f = _m(
        FactKind.TYPE_NOTE,
        c("orders", "order_id"),
        {"stored_as": "integer", "means": "epoch seconds", "rate": 1.0, "holds": True},
    )
    with shop_store(tmp_path / "s.db", [*shop_facts(), f]) as s:
        text = AGENT.render_table(collect(s, t("orders"), NOW, CONFIG), DetailLevel.FULL)
    line = next(ln for ln in text.splitlines() if ln.startswith("    order_id"))
    assert "holds epoch seconds (see warning)" in line and "text holding" not in line
    assert "text holding ISO-8601 timestamp" in text  # created_at is VARCHAR


def test_identifiers_quoted_for_reserved_and_mixed_case(tmp_path: Path) -> None:
    from schemaguru.render.table_block import ident, qname

    assert ident("select") == '"select"'
    assert ident("Orders") == '"Orders"'
    assert ident("order_id") == "order_id"
    assert qname(ObjectRef(None, "Shop", "user", "id")) == '"Shop"."user".id'
    ref = ObjectRef(None, "shop", "Orders")
    with SQLiteStore(tmp_path / "q.db") as s:
        s.upsert_tables([Table(ref, "table")])
        s.upsert_columns(
            ref, [Column(ObjectRef(None, "shop", "Orders", "select"), 1, "INT", "integer")]
        )
        text = AGENT.render_table(collect(s, ref, NOW, CONFIG), DetailLevel.FULL)
    assert text.startswith('[shop."Orders"]') and '"select"' in text


def test_human_and_declared_expectations_say_unchecked(store: SQLiteStore) -> None:
    sessions = block(store, "sessions")
    assert "note: UTC start (human, unchecked)" in sessions
    assert "(human)" not in sessions
