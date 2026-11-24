"""Snapshots: a thin wrapper over the store (spec 7.1, 15)."""

from __future__ import annotations

from collections.abc import Iterable

from schemaguru.store.protocol import Store


def take(store: Store, checks_run: Iterable[str] = frozenset(), label: str | None = None) -> str:
    """Freeze the store's facts; ``checks_run`` records what this run measured.

    A later diff must read a check missing from ``checks_run`` as "not measured", never as
    "fixed" (spec 15.4).
    """
    return store.snapshot(label, checks_run=checks_run)
