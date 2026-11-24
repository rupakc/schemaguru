"""The value policy in rendering (spec 11): defence in depth, whatever the store holds."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from schemaguru.config import Config, SamplingConfig
from schemaguru.model.detail import DetailLevel
from schemaguru.model.kinds import FactKind
from schemaguru.render import ViewRenderer, collect, get_profile, profile_names
from schemaguru.store.sqlite import SQLiteStore
from tests.golden.golden import assert_golden
from tests.golden.shop_knowledge import CONFIG, NOW, c, human, measured, shop_facts, shop_store, t

SECRETS = ("alice@example.com", "bob@example.com", "carol@example.com", "dave@example.com")


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
                "distinct": 3,
                "non_null": 3,
                "observed": [
                    {"value": "alice@example.com", "count": 1},
                    {"value": "Alice@Example.com", "count": 1},
                    {"value": "bob@example.com", "count": 1},
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
    view = ViewRenderer(store, now=NOW, cfg=config).view(
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
