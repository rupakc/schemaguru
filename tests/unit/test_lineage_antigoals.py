"""Lineage anti-goals (0.2.0 spec 2): lineage is never a join path, never widens scoping, never
reaches the join graph or a SQL snippet, and is never VERIFIED."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from schemaguru.graph.search import Refusal
from schemaguru.model.evidence import Evidence
from schemaguru.model.facts import Fact, Provenance, Status
from schemaguru.model.kinds import FactKind
from schemaguru.model.lineage import table_lineage_value
from schemaguru.model.refs import ObjectRef
from tests.unit.lineage_helpers import alpha_beta_guru


def test_lineage_ties_the_pair(tmp_path: Path) -> None:
    """The precondition every test here relies on: lineage does link the two tables."""
    with alpha_beta_guru(tmp_path) as guru:
        r = guru.lineage("shop.beta")
        assert [(e.upstream, e.downstream) for e in r.edges] == [("shop.alpha", "shop.beta")]


def test_find_path_refuses(tmp_path: Path) -> None:
    with alpha_beta_guru(tmp_path) as guru:
        res = guru.find_path("shop.alpha", "shop.beta")
        assert res.paths == () and isinstance(res.refusal, Refusal)
        assert guru.find_path("shop.beta", "shop.alpha").refusal is not None


def test_scope_does_not_add_upstream(tmp_path: Path) -> None:
    with alpha_beta_guru(tmp_path) as guru:
        view = guru.context("alpha")
        tables = [t.canonical() for t in view.tables]
        assert "shop.alpha" in tables  # the control: the question does scope alpha
        assert "shop.beta" not in tables
        assert "shop.alpha" not in [t.canonical() for t in guru.context("beta").tables]


def test_graph_reads_no_lineage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with alpha_beta_guru(tmp_path) as guru:
        kinds: list[Any] = []
        real = guru.store.facts_for

        def spy(subjects: Any, ks: Any) -> Any:
            kinds.append(None if ks is None else list(ks))
            return real(subjects, ks)

        monkeypatch.setattr(guru.store, "facts_for", spy)
        guru._current_graph()
        assert kinds == [["JOIN", "ENTITY_TYPE"]]


def test_snippet_unaffected(tmp_path: Path) -> None:
    with alpha_beta_guru(tmp_path) as guru:
        out = guru.handle_tool_call("find_join_path", {"from": "shop.alpha", "to": "shop.beta"})
        assert out["refusal"] is not None and out["paths"] == []
        text = out["text"].upper()
        assert " ON " not in text and "SELECT" not in text and "=" not in text  # no snippet


def test_lineage_is_never_verified() -> None:
    value = table_lineage_value(["shop.alpha"])
    beta = ObjectRef(None, "shop", "beta")
    ev = Evidence(verdict="PASS", claim_text="x", sql="SELECT 1")
    for prov in (Provenance.HUMAN, Provenance.DECLARED, Provenance.CATALOG):
        with pytest.raises(ValueError):
            Fact.make(FactKind.LINEAGE, beta, value, prov, Status.VERIFIED, evidence=ev,
                      method="exact")  # fmt: skip
