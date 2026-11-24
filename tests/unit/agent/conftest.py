"""Fixtures for the agent surface: a measured shop Guru and a Guru over planted secrets."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from schemaguru.catalog import DBAPIExecutor, reader_for
from schemaguru.config import Config, SamplingConfig
from schemaguru.guru import Guru
from schemaguru.measure import flbridge
from schemaguru.model.evidence import Evidence
from tests.fixtures.shop import ShopFacts
from tests.golden.shop_knowledge import CONFIG, NOW, shop_store
from tests.unit.agent.planted import planted_facts
from tests.unit.measure.fake_backend import FakeBackend

OVERLAY = """\
version: 1
tables:
  shop.orders:
    description: One row per order placed on the storefront.
    use_cases: [refund rate by month, revenue by month]
    columns:
      status:
        values: [paid, refunded, cancelled]
glossary:
  - term: refund rate
    definition: refunded orders over all orders
    maps_to: [shop.orders]
"""

CHECK_SPEC = {
    "name": "factlang_check",
    "description": "Check FactLang claims against the warehouse.",
    "input_schema": {
        "type": "object",
        "properties": {"claims": {"type": "string"}},
        "required": ["claims"],
    },
}


class CheckingBackend(FakeBackend):  # type: ignore[misc]
    """A fake backend whose ``check`` returns canned evidence (or raises)."""

    def __init__(self, conn: Any, evidence: list[Evidence] | Exception | None = None) -> None:
        super().__init__(conn)
        self.evidence = evidence if evidence is not None else []
        self.checked: list[str] = []

    def check(self, claims: str) -> list[Evidence]:
        self.checked.append(claims)
        if isinstance(self.evidence, Exception):
            raise self.evidence
        return list(self.evidence)


@pytest.fixture
def seam_check(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Pretend the installed FactLang publishes its ``factlang_check`` agent tool."""
    monkeypatch.setattr(flbridge, "check_tool_definition", lambda: dict(CHECK_SPEC))
    return CHECK_SPEC


@pytest.fixture
def no_seam_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(flbridge, "check_tool_definition", lambda: None)


def make_shop_guru(
    tmp_path: Path, conn: Any, *, backend: Any = None, config: Config | None = None
) -> Guru:
    g = Guru(
        reader=reader_for("duckdb", DBAPIExecutor(conn)),
        backend=backend if backend is not None else CheckingBackend(conn),
        store=str(tmp_path / "guru.db"),
        config=config,
        dialect="duckdb",
    )
    g.crawl()
    p = tmp_path / "o.yaml"
    p.write_text(OVERLAY, encoding="utf-8")
    g.load_overlays(p)
    g.measure(["shop.orders", "shop.customers"])
    return g


@pytest.fixture
def shop_guru(tmp_path: Path, shop_duckdb: tuple[Any, ShopFacts]) -> Iterator[Guru]:
    g = make_shop_guru(tmp_path, shop_duckdb[0])
    yield g
    g.close()


def mode_config(mode: str, **per_glob: str) -> Config:
    return replace(CONFIG, sampling=SamplingConfig(llm_values=mode, per_glob=per_glob))  # type: ignore[arg-type]


@pytest.fixture
def planted(tmp_path: Path) -> Iterator[Callable[..., Guru]]:
    """``planted(mode, backend=None)``: a Guru over the shop knowledge plus planted secrets."""
    with shop_store(tmp_path / "planted.db", planted_facts()) as store:
        made: list[Guru] = []

        def make(mode: str, backend: Any = None, **per_glob: str) -> Guru:
            g = Guru(
                store=store, backend=backend, config=mode_config(mode, **per_glob), now=lambda: NOW
            )
            made.append(g)
            return g

        yield make
