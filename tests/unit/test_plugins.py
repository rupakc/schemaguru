"""Entry-point discovery (spec 21): ``schemaguru.<point>`` groups load lazily, once, on first
registry use; a broken plugin is skipped with ``SG108`` and never raises (spec 2 G7)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from schemaguru import plugins
from schemaguru.catalog import registry
from schemaguru.feedback import classifiers as fb
from schemaguru.measure import candidates
from schemaguru.model import kinds
from schemaguru.model.kinds import KindSpec
from schemaguru.pii import detectors
from schemaguru.refresh import classify
from schemaguru.render import formats, profiles


class EP:
    """A stand-in for ``importlib.metadata.EntryPoint``."""

    def __init__(self, name: str, obj: Any = None, error: Exception | None = None) -> None:
        self.name, self._obj, self._error = name, obj, error
        self.loads = 0

    def load(self) -> Any:
        self.loads += 1
        if self._error is not None:
            raise self._error
        return self._obj


@pytest.fixture
def installed(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, list[EP]]]:
    """Fake installed entry points per group, with every registry restored afterwards."""
    eps: dict[str, list[EP]] = {}
    monkeypatch.setattr(plugins, "_entry_points", lambda group: eps.get(group, []))
    monkeypatch.setattr(plugins, "_done", set())
    monkeypatch.setattr(plugins, "_diags", [])
    monkeypatch.setattr(plugins, "_named", {p: {} for p in plugins.NAMED_POINTS})
    registry._load_builtin()  # built-ins first, so the plugin table starts from them
    monkeypatch.setattr(registry, "_FACTORIES", dict(registry._FACTORIES))
    monkeypatch.setattr(detectors, "_registry", list(detectors._registry))
    monkeypatch.setattr(kinds, "_REGISTRY", dict(kinds._REGISTRY))
    monkeypatch.setattr(profiles, "_PROFILES", dict(profiles._PROFILES))
    monkeypatch.setattr(formats, "_FORMATS", dict(formats._FORMATS))
    monkeypatch.setattr(classify, "_CUSTOM", list(classify._CUSTOM))
    monkeypatch.setattr(fb, "_PLUGINS", [])
    monkeypatch.setattr(candidates, "_SOURCES", [])
    monkeypatch.setattr(plugins, "_done", set())  # discover again after the warm-up above
    yield eps


def test_every_point_has_a_group() -> None:
    assert all(g == f"schemaguru.{p}" for p, g in plugins.GROUPS.items())
    assert len(plugins.GROUPS) == 14


def test_reader_plugin_is_found_lazily_and_once(installed: dict[str, list[EP]]) -> None:
    made: list[Any] = []

    def factory(executor: Any) -> str:
        made.append(executor)
        return "toy-reader"

    ep = EP("toyshop", factory)
    installed["schemaguru.readers"] = [ep]
    assert ep.loads == 0  # nothing happens before a registry is used
    assert "toyshop" in registry.dialects()
    assert registry.reader_for("toyshop", "exe") == "toy-reader"  # type: ignore[arg-type]
    registry.dialects()
    assert ep.loads == 1 and made == ["exe"]


def test_broken_plugins_are_skipped_with_sg108(installed: dict[str, list[EP]]) -> None:
    installed["schemaguru.readers"] = [
        EP("boom", error=ImportError("no module named toyshop_sg")),
        EP("duckdb", lambda e: None),  # a built-in name: refused, not replaced
        EP("nonsense", 42),  # not a factory
        EP("fine", lambda e: "ok"),
    ]
    names = registry.dialects()  # never raises
    assert "fine" in names and "boom" not in names and "nonsense" not in names
    assert registry.reader_for("duckdb", object()).__class__.__name__ == "DuckDBReader"  # type: ignore[arg-type]
    diags = plugins.diagnostics()
    assert [d.code for d in diags] == ["SG108"] * 3
    assert {d.location for d in diags} == {"schemaguru.readers"}
    assert "ImportError" in diags[0].message and "'boom'" in diags[0].message


def test_metadata_failure_is_a_diagnostic(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(group: str) -> list[Any]:
        raise RuntimeError("corrupt metadata")

    monkeypatch.setattr(plugins, "_entry_points", broken)
    monkeypatch.setattr(plugins, "_done", set())
    monkeypatch.setattr(plugins, "_diags", [])
    plugins.discover("formats", lambda n, o: None)
    assert [d.code for d in plugins.diagnostics()] == ["SG108"]


class Sku:
    name = "sku"
    category = "other"

    def matches(self, value: str) -> bool:
        return value.startswith("SKU-")


class NoOpinion:
    def classify(self, *a: Any) -> None:
        return None


class Always:
    def classify(self, sql: str, error: str, dialect: str | None) -> list[Any]:
        return [fb.FeedbackRecord(None, None, "unknown_column")]


class Proposer:
    name = "proposer"

    def candidates(self, store: Any, table: Any) -> list[Any]:
        return []


@pytest.mark.parametrize(
    ("group", "entry", "check"),
    [
        ("pii_detectors", EP("sku", Sku), lambda: detectors.first_match("SKU-1") is not None),
        (
            "fact_kinds",
            EP("CARBON", KindSpec("CARBON", "table", False, False)),
            lambda: kinds.get_kind("CARBON").name == "CARBON",
        ),
        (
            "profiles",
            EP("tiny", profiles.RouterProfile),
            lambda: isinstance(profiles.get_profile("tiny"), profiles.RouterProfile),
        ),
        ("formats", EP("shout", lambda v: "X"), lambda: formats.format_view(None, "shout") == "X"),  # type: ignore[arg-type]
        (
            "change_classifiers",
            EP("quiet", NoOpinion),
            lambda: any(isinstance(c, NoOpinion) for c in classify.change_classifiers()),
        ),
        (
            "failure_classifiers",
            EP("always", Always),
            lambda: any(isinstance(c, Always) for c in fb.default_classifiers()),
        ),
        (
            "join_candidate_sources",
            EP("proposer", Proposer),
            lambda: [s.name for s in candidates.join_candidate_sources()] == ["proposer"],
        ),
    ],
)
def test_each_registry_discovers_its_group(
    installed: dict[str, list[EP]], group: str, entry: EP, check: Any
) -> None:
    installed[f"schemaguru.{group}"] = [entry]
    assert check() and entry.loads == 1
    assert plugins.diagnostics() == ()


@pytest.mark.parametrize("point", sorted(plugins.NAMED_POINTS))
def test_named_points_are_filled_from_entry_points(
    installed: dict[str, list[EP]], point: str
) -> None:
    factory = object()
    installed[f"schemaguru.{point}"] = [EP("mine", factory), EP("bad", error=SyntaxError("x"))]
    assert plugins.get(point, "mine") is factory
    assert plugins.names(point) == ["mine"]
    assert [d.code for d in plugins.diagnostics()] == ["SG108"]
    with pytest.raises(KeyError, match="registered: mine"):
        plugins.get(point, "other")


def test_named_registry_rejects_registry_points_and_duplicates(
    installed: dict[str, list[EP]],
) -> None:
    with pytest.raises(ValueError, match="own registry"):
        plugins.register("readers", "x", object())
    plugins.register("stores", "a", 1)
    with pytest.raises(ValueError, match="already registered"):
        plugins.register("stores", "a", 2)
    plugins.register("stores", "a", 2, replace=True)
    assert plugins.get("stores", "a") == 2
