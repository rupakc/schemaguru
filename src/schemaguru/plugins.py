"""Extension discovery through entry points (layer 0, spec 21).

An installed package extends SchemaGuru by declaring entry points in a group
``schemaguru.<point>``; each registry loads its group lazily, once, on first use::

    [project.entry-points."schemaguru.readers"]
    toyshop = "toyshop_sg:make_reader"

A plugin that fails to import, or that its registry refuses (a duplicate name, a wrong type),
is skipped with an ``SG108`` diagnostic (:func:`diagnostics`); it never raises (spec 2 G7).

Points whose implementation is passed to a constructor (a store, a retriever, an LLM provider)
have no registry of their own: :func:`register` / :func:`get` keep them by name, and the same
entry-point groups fill those tables.
"""

from __future__ import annotations

import importlib.metadata
import threading
from collections.abc import Callable
from typing import Any

# extension point -> entry-point group (spec 21: ``schemaguru.<point>``)
GROUPS: dict[str, str] = {
    point: f"schemaguru.{point}"
    for point in (
        "readers",
        "stores",
        "retrievers",
        "embedders",
        "token_counters",
        "llm_providers",
        "pii_detectors",
        "failure_classifiers",
        "fact_kinds",
        "profiles",
        "formats",
        "change_classifiers",
        "join_candidate_sources",
        "authenticators",
    )
}
# points without a registry of their own: kept here by name
NAMED_POINTS = frozenset(
    {"stores", "retrievers", "embedders", "token_counters", "llm_providers", "authenticators"}
)

_lock = threading.RLock()
_done: set[str] = set()
# SG108 Diagnostics, in load order (schemaguru.diagnostics is a same-layer module: it is imported
# inside the functions that build them, spec 5.1)
_diags: list[Any] = []
_named: dict[str, dict[str, Any]] = {p: {} for p in NAMED_POINTS}


def _entry_points(group: str) -> list[Any]:
    """The installed entry points of ``group`` (tests replace this function)."""
    return list(importlib.metadata.entry_points(group=group))


def discover(point: str, register: Callable[[str, Any], None]) -> None:
    """Load ``schemaguru.<point>`` once and hand each plugin to ``register(name, obj)``.

    Cheap after the first call. Anything a plugin or ``register`` raises becomes ``SG108``.
    """
    if point in _done:
        return
    with _lock:
        if point in _done:
            return
        _done.add(point)  # first, so a plugin that touches its own registry cannot recurse
        group = GROUPS[point]
        try:
            eps = sorted(_entry_points(group), key=lambda ep: str(getattr(ep, "name", "")))
        except Exception as e:  # broken package metadata: nothing from this group
            _note(group, "*", e)
            return
        for ep in eps:
            name = str(getattr(ep, "name", "?"))
            try:
                register(name, ep.load())
            except Exception as e:
                _note(group, name, e)


def _note(group: str, name: str, e: Exception) -> None:
    from schemaguru.diagnostics import Diagnostic, Severity

    text = str(e).splitlines()[0][:200] if str(e) else ""
    _diags.append(
        Diagnostic(
            "SG108",
            Severity.WARNING,
            f"plugin {name!r} ({group}) was skipped: {type(e).__name__}"
            + (f": {text}" if text else ""),
            group,
            "fix or uninstall the package that declares it",
        )
    )


def diagnostics() -> tuple[Any, ...]:
    """Every ``SG108`` so far (plugins skipped while loading)."""
    with _lock:
        return tuple(_diags)


def reset() -> None:
    """Forget what was loaded (tests): the next registry use discovers again."""
    with _lock:
        _done.clear()
        _diags.clear()
        for table in _named.values():
            table.clear()


def register(point: str, name: str, factory: Any, *, replace: bool = False) -> None:
    """Keep ``factory`` under ``name`` for a constructor-injected point (``stores``, …)."""
    if point not in NAMED_POINTS:
        raise ValueError(f"{point!r} has its own registry; named points: {sorted(NAMED_POINTS)}")
    table = _named[point]
    with _lock:
        if name in table and not replace:
            raise ValueError(f"{point} plugin already registered: {name}")
        table[name] = factory


def _load_named(point: str) -> None:
    discover(point, lambda name, obj: register(point, name, obj))


def get(point: str, name: str) -> Any:
    """The factory registered (in code or by an entry point) as ``name`` for ``point``."""
    if point not in NAMED_POINTS:
        raise ValueError(f"{point!r} has its own registry; named points: {sorted(NAMED_POINTS)}")
    _load_named(point)
    try:
        return _named[point][name]
    except KeyError:
        known = ", ".join(sorted(_named[point])) or "none"
        raise KeyError(f"no {point} plugin named {name!r} (registered: {known})") from None


def names(point: str) -> list[str]:
    """Registered names of a constructor-injected point."""
    _load_named(point)
    return sorted(_named[point])


def instance(obj: Any) -> Any:
    """A plugin may name a class or an instance: a class is instantiated with no arguments."""
    return obj() if isinstance(obj, type) else obj


__all__ = [
    "GROUPS",
    "NAMED_POINTS",
    "diagnostics",
    "discover",
    "get",
    "instance",
    "names",
    "register",
    "reset",
]
