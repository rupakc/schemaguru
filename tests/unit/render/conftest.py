"""Shared fixtures for the render tests."""

from __future__ import annotations

import pytest

from schemaguru.render import fragments


class _Inline:
    def __init__(self, text: str) -> None:
        self.text = text

    def inline(self) -> str:
        return self.text


@pytest.fixture
def fake_factlang(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """A FactLang with ``parse_filter`` and ``sql.render_join``/``JoinEdge`` (real signatures)."""
    import sys
    import types

    import factlang

    seen: dict[str, object] = {}

    def parse_filter(text: str) -> tuple[object, list[object]]:
        if "(((" in text:
            return None, [types.SimpleNamespace(severity="ERROR", message="bad")]
        return text, []

    def render_join(
        edges: object, dialect: str, *, filters: object = None, aliases: object = None
    ) -> _Inline:
        seen.update(edges=edges, dialect=dialect, filters=filters, aliases=aliases)
        return _Inline("FROM <join>")

    refs = types.ModuleType("factlang.semantics.refs")
    refs.Ident = lambda text, quoted: text  # type: ignore[attr-defined]
    refs.TableRef = lambda *parts: tuple(parts)  # type: ignore[attr-defined]
    refs.ColumnRef = lambda table, column: (table, column)  # type: ignore[attr-defined]
    refs.EdgeRef = lambda left, right: (left, right)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "factlang.semantics", types.ModuleType("factlang.semantics"))
    monkeypatch.setitem(sys.modules, "factlang.semantics.refs", refs)
    sql = types.ModuleType("factlang.sql")
    sql.render_join = render_join  # type: ignore[attr-defined]
    sql.JoinEdge = lambda left, right, cast_to=None: (left, right, cast_to)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "factlang.sql", sql)
    monkeypatch.setattr(factlang, "parse_filter", parse_filter, raising=False)
    monkeypatch.setattr(fragments, "_capable", lambda name: True)
    return seen
