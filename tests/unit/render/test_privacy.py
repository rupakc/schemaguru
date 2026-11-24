"""The value policy in rendering (spec 11): defence in depth, whatever the store holds."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from schemaguru.config import Config, SamplingConfig
from schemaguru.model.detail import DetailLevel
from schemaguru.model.facts import Fact, Provenance, Status
from schemaguru.model.kinds import FactKind
from schemaguru.overlays.schema import GLOBAL_SUBJECT
from schemaguru.render import ViewRenderer, collect, get_profile, profile_names
from schemaguru.store.sqlite import SQLiteStore
from tests.golden.golden import assert_golden
from tests.golden.shop_knowledge import (
    CONFIG,
    MEASURED_AT,
    NOW,
    c,
    human,
    measured,
    shop_facts,
    shop_graph,
    shop_store,
    t,
)

SECRETS = (
    "alice@example.com",
    "bob@example.com",
    "carol@example.com",
    "dave@example.com",
    # reviewer probes: free text and SQL, not values
    "a1@example.com",
    "h8@example.com",
    "c3@example.com",
    "d4@example.com",
    "e5@example.com",
    "f6@example.com",
    "g7@example.com",
    "k9@example.com",
)
PARTIAL = "Zebulon Quartzfeld"


def pii_facts() -> list:  # type: ignore[type-arg]
    return [
        *shop_facts(),
        # stored values that were never masked (an older store, a bypassed writer)
        measured(
            FactKind.VALUES,
            c("customers", "email"),
            {
                "case_sensitive": True,
                "split": None,
                "distinct": 4,
                "non_null": 4,
                "observed": [
                    {"value": "alice@example.com", "count": 1},
                    {"value": "Alice@Example.com", "count": 1},
                    {"value": "bob@example.com", "count": 1},
                    # a partial detector hit on a PII column: the rest must not leak either
                    {"value": "Zebulon Quartzfeld a1@example.com", "count": 1},
                ],
            },
        ),
        # a human declared a list of addresses on a PII column
        human(
            FactKind.VALUES,
            c("customers", "phone"),
            {
                "values": ["+15550000001", "carol@example.com"],
                "case_sensitive": True,
                "split": None,
            },
        ),
        # a non-PII column whose value embeds an address: masked in place by the detector
        measured(
            FactKind.VALUES,
            c("accounts", "name"),
            {
                "case_sensitive": True,
                "split": None,
                "distinct": 2,
                "non_null": 100,
                "observed": [
                    {"value": "account 1", "count": 99},
                    {"value": "ask dave@example.com", "count": 1},
                ],
            },
        ),
        measured(FactKind.PII, c("customers", "phone"), {"category": "phone", "detector": "phone"}),
        # a probe error whose message quotes a value
        Fact.make(
            FactKind.NULL_RATE,
            c("customers", "segment"),
            {"rate": None, "reason": "cast failed for 'h8@example.com'"},
            Provenance.MEASURED,
            Status.ERROR,
            method="exact",
            observed_at=MEASURED_AT,
        ),
        # a live-rows predicate with a literal
        human(
            FactKind.SOFT_DELETE,
            t("customers"),
            {"predicate": "email <> 'c3@example.com'", "marker_columns": None},
        ),
        human(FactKind.NEVER_QUERY, t("accounts"), {"reason": "owned by d4@example.com"}),
        human(
            FactKind.PITFALL,
            t("customers"),
            {"rule": "ask e5@example.com before use", "fix": None, "severity": "warn"},
        ),
        human(FactKind.DESCRIPTION, t("accounts"), {"text": "Accounts; contact f6@example.com."}),
        human(FactKind.DESCRIPTION, c("customers", "segment"), {"text": "set by k9@example.com"}),
        human(
            FactKind.GLOSSARY,
            GLOBAL_SUBJECT,
            {
                "term": "segment",
                "definition": "see g7@example.com",
                "maps_to": [t("customers").to_dict()],
            },
        ),
    ]


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SQLiteStore]:
    with shop_store(tmp_path / "pii.db", pii_facts()) as s:
        yield s


def cfg(mode: str, **per_glob: str) -> Config:
    return replace(CONFIG, sampling=SamplingConfig(llm_values=mode, per_glob=per_glob))  # type: ignore[arg-type]


def everything(store: SQLiteStore, config: Config) -> str:
    out = []
    for name in ("customers", "accounts", "orders"):
        k = collect(store, t(name), NOW, config)
        for p in profile_names():
            for level in DetailLevel:
                out.append(get_profile(p).render_table(k, level))
    for profile in profile_names():
        view = ViewRenderer(store, now=NOW, cfg=config, profile=profile).view(
            "q", [(t("customers"), DetailLevel.FULL), (t("accounts"), DetailLevel.FULL)]
        )
        out += [view.text, view.to_json(), view.to_xml(), view.to_markdown()]
    return "\n".join(out)


@pytest.mark.parametrize("mode", ["masked", "none"])
def test_no_personal_value_reaches_the_agent(store: SQLiteStore, mode: str) -> None:
    text = everything(store, cfg(mode))
    for secret in SECRETS:
        assert secret not in text, (mode, secret)
        assert secret.split("@")[0] + "@" not in text.lower(), (mode, secret)
    assert PARTIAL not in text, mode  # a partial hit on a PII column hides the whole value
    # the hazards are still there, with their personal parts masked
    quoted = "'<email>'" if mode == "masked" else "?"  # none: a quoted literal is not shown
    assert f"measurement failed (cast failed for {quoted}" in text
    assert "⚠ shop.accounts — owned by <email>" in text
    assert "Pitfall: ask <email> before use" in text
    assert "segment: see <email>" in text


def test_masked_mode_shows_shape_tokens_and_keeps_hazards(store: SQLiteStore) -> None:
    k = collect(store, t("customers"), NOW, cfg("masked"))
    text = get_profile("agent").render_table(k, DetailLevel.FULL)
    assert "values: <email> (1), <email> (1), <email> (1)" in text
    # case drift among masked values is still reported, as a count
    assert "⚠ email: case drift: 2 values (values hidden by policy) differ only in case" in text
    # a declared list on a PII column: each value masked
    assert "values: <phone>,<email>" in text
    accounts = get_profile("agent").render_table(
        collect(store, t("accounts"), NOW, cfg("masked")), DetailLevel.FULL
    )
    assert "'account 1' (99), 'ask <email>' (1)" in accounts


def test_none_mode_shows_counts_only(store: SQLiteStore) -> None:
    config = cfg("none")
    for name in ("customers", "orders", "accounts"):
        k = collect(store, t(name), NOW, config)
        text = get_profile("agent").render_table(k, DetailLevel.FULL)
        verifier = get_profile("verifier").render_table(k, DetailLevel.FULL)
        for line in (text + "\n" + verifier).splitlines():
            if "values" in line.lower() and "'" in line:
                raise AssertionError(f"a value leaked in none mode: {line}")
    orders = get_profile("agent").render_table(
        collect(store, t("orders"), NOW, config), DetailLevel.FULL
    )
    assert "values: 3 declared (values hidden by policy) (+ 1 more observed)" in orders
    assert "observed also 1 undeclared value(s) (412 rows)" in orders
    assert "holds TRUE" not in orders  # marker values are values too
    customers = get_profile("agent").render_table(
        collect(store, t("customers"), NOW, config), DetailLevel.FULL
    )
    assert "values: 3 distinct (values hidden by policy)" in customers


def test_raw_is_an_explicit_opt_in_per_glob(store: SQLiteStore) -> None:
    k = collect(store, t("customers"), NOW, cfg("masked", **{"shop.customers": "raw"}))
    assert "'alice@example.com'" in get_profile("agent").render_table(k, DetailLevel.FULL)
    k2 = collect(store, t("customers"), NOW, cfg("masked", **{"shop.orders": "raw"}))
    assert "alice@example.com" not in get_profile("agent").render_table(k2, DetailLevel.FULL)


@pytest.mark.parametrize("mode", ["masked", "none"])
def test_pii_golden(store: SQLiteStore, mode: str) -> None:
    k = collect(store, t("customers"), NOW, cfg(mode))
    text = "\n\n".join(
        get_profile(p).render_table(k, DetailLevel.FULL) for p in ("agent", "verifier")
    )
    assert_golden(f"customers__pii_{mode}.txt", text)


def test_partial_hit_on_a_pii_column_is_masked_whole(store: SQLiteStore) -> None:
    text = get_profile("agent").render_table(
        collect(store, t("customers"), NOW, cfg("masked")), DetailLevel.FULL
    )
    assert "<email> (1), <email> (1), <email> (1), <masked> (1)" in text


def test_predicate_literals_masked_or_hidden(store: SQLiteStore) -> None:
    masked = get_profile("agent").render_table(
        collect(store, t("customers"), NOW, cfg("masked")), DetailLevel.FULL
    )
    assert "live rows: (email <> '<email>') (human, unchecked)" in masked
    hidden = get_profile("verifier").render_table(
        collect(store, t("customers"), NOW, cfg("none")), DetailLevel.FULL
    )
    assert "SOFT_DELETE (email <> ?) human unchecked" in hidden


@pytest.mark.parametrize("mode", ["masked", "none"])
def test_snippet_literals_follow_the_policy(
    store: SQLiteStore,
    fake_factlang: dict,
    mode: str,  # type: ignore[type-arg]
) -> None:
    r = ViewRenderer(store, now=NOW, cfg=cfg(mode), graph=shop_graph(store), dialect="duckdb")
    view = r.view("q", [(t("orders"), DetailLevel.LITE), (t("customers"), DetailLevel.LITE)])
    passed = {str(v) for v in fake_factlang["filters"].values()}  # type: ignore[union-attr]
    expected = "email <> '<email>'" if mode == "masked" else "email <> ?"
    assert expected in passed and not any("c3@" in p for p in passed)
    assert "c3@" not in view.text


def test_view_counts_what_it_masked_outside_the_text(store: SQLiteStore) -> None:
    view = ViewRenderer(store, now=NOW, cfg=cfg("masked")).view(
        "q", [(t("customers"), DetailLevel.FULL), (t("accounts"), DetailLevel.FULL)]
    )
    (d,) = [d for d in view.diagnostics if d.code == "SG505"]
    assert d.message.startswith("value policy: ") and "masked in this view" in d.message
    assert int(d.message.split()[2]) > 0
    assert "SG505" not in view.text and "masked in this view" not in view.text


@pytest.mark.parametrize("mode", ["masked", "none"])
def test_identifiers_are_never_masked(tmp_path: Path, mode: str) -> None:
    from schemaguru.model.refs import ObjectRef
    from schemaguru.model.structure import Column, Table

    ref = ObjectRef(None, "shop", "Order Items")
    cols = [
        Column(ObjectRef(None, "shop", "Order Items", n), i, "VARCHAR", "string")
        for i, n in enumerate(("customer_name", "select", "email_address"), 1)
    ]
    with SQLiteStore(tmp_path / "ids.db") as s:
        s.upsert_tables([Table(ref, "table")])
        s.upsert_columns(ref, cols)
        s.put_facts(
            [
                measured(
                    FactKind.PII,
                    ObjectRef(None, "shop", "Order Items", "customer_name"),
                    {"category": "name", "detector": "name"},
                )
            ]
        )
        k = collect(s, ref, NOW, cfg(mode))
        for p in profile_names():
            for level in DetailLevel:
                text = get_profile(p).render_table(k, level)
                assert 'shop."Order Items"' in text, (p, level)
                if level is not DetailLevel.NAME_ONLY and p in ("agent", "verifier"):
                    assert "customer_name" in text and '"select"' in text, (p, level)
                    assert "email_address" in text, (p, level)
