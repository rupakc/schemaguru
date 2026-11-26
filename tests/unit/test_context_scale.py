"""``Guru.context`` reads what the scope needs, not the whole store (0.2.0 spec 6.1).

At 2,000 synthetic tables ``context`` p95 was 259 ms against a 200 ms target, and it grew with
the store: every call decoded (and re-verified) every JOIN fact in the store to find the few that
touch the tables in scope. These tests pin the slow path at a small size: the facts a context
call decodes must not grow with join facts on tables the question never reaches.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from benchmarks.scale.generator import ScaleShape, SyntheticReader, synthetic_join_facts

from schemaguru.catalog import crawl
from schemaguru.guru import Guru
from schemaguru.model.facts import Fact
from schemaguru.store.sqlite import SQLiteStore

QUESTION = "orders amount by customer"


def _store(path: Path, tables: int) -> SQLiteStore:
    shape = replace(ScaleShape(), tables=tables, columns_per_table=4, tables_per_schema=100)
    store = SQLiteStore(path)
    crawl(SyntheticReader(shape), store)
    store.put_facts(list(synthetic_join_facts(shape)))
    return store


class _Counting:
    """Counts ``Fact.from_dict`` (every fact a read decodes) and whole-kind JOIN scans."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, store: SQLiteStore) -> None:
        self.decoded = 0
        self.join_scans = 0
        real_from_dict = Fact.from_dict.__func__  # type: ignore[attr-defined]
        real_facts_for = store.facts_for

        def from_dict(cls: type[Fact], d: Any) -> Fact:
            self.decoded += 1
            return real_from_dict(cls, d)  # type: ignore[no-any-return]

        def facts_for(subjects: Any, kinds: Any) -> list[Fact]:
            if subjects is None and kinds is not None and "JOIN" in kinds:
                self.join_scans += 1
            return real_facts_for(subjects, kinds)

        monkeypatch.setattr(Fact, "from_dict", classmethod(from_dict))
        monkeypatch.setattr(store, "facts_for", facts_for)


def _decoded_by_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tables: int
) -> tuple[int, int]:
    store = _store(tmp_path / f"s{tables}.db", tables)
    guru = Guru(store=store)
    try:
        guru._current_graph()  # built once per knowledge generation, not per question
        with monkeypatch.context() as m:
            counting = _Counting(m, store)
            guru.context(QUESTION)
        return counting.decoded, counting.join_scans
    finally:
        guru.close()
        store.close()


@pytest.fixture
def small_and_large(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    yield (
        _decoded_by_context(tmp_path, monkeypatch, 300),
        _decoded_by_context(tmp_path, monkeypatch, 1200),
    )


def test_context_does_not_scan_every_join(small_and_large: Any) -> None:
    (_, scans_small), (_, scans_large) = small_and_large
    assert scans_small == 0
    assert scans_large == 0


def test_context_decodes_facts_in_scope_only(small_and_large: Any) -> None:
    (small, _), (large, _) = small_and_large
    assert small > 0
    # four times the store, the same scope: the decoded facts must not follow the store's size
    assert large <= small * 1.5, (small, large)


def test_a_tampered_join_fact_is_still_detected(tmp_path: Path) -> None:
    """The index only says which subjects to read: every JOIN fact a context call reads is
    still decoded with its id re-verified (``Fact.from_dict``)."""
    store = _store(tmp_path / "t.db", 300)
    guru = Guru(store=store)
    try:
        guru._current_graph()  # the index is built from untampered facts
        store._conn.execute(
            "UPDATE facts SET data = json_set(data, '$.id', '0' || id) WHERE kind = 'JOIN'"
        )
        with pytest.raises(ValueError, match="fact id mismatch"):
            guru.context(QUESTION)
    finally:
        guru.close()
        store.close()


def _new_join(child: Any, parent: Any) -> Fact:
    from schemaguru.model.facts import Provenance, Status
    from schemaguru.model.joins import JOIN_VALUE_KEYS, JoinRef
    from schemaguru.model.kinds import FactKind
    from schemaguru.model.refs import ObjectRef

    join = JoinRef(
        (ObjectRef(child.catalog, child.schema, child.table, "updated_3"),),
        (ObjectRef(parent.catalog, parent.schema, parent.table, "id"),),
    )
    value: dict[str, Any] = {key: None for key in JOIN_VALUE_KEYS}
    value.update(match_rate=0.99, coverage=0.5, matched=99, left_non_null=100, verdict="OK")
    return Fact.make(
        FactKind.JOIN, join, value, Provenance.MEASURED, Status.OBSERVED, method="exact"
    )


def test_a_join_written_by_another_process_reaches_the_view(tmp_path: Path) -> None:
    """The per-table JOIN index must not hide a join measured after it was built: here by a
    second store object on the same file (another process, a worker), which never tells this
    ``Guru``. Before the index every renderer read every JOIN fact, so it was always shown."""
    from benchmarks.scale.generator import table_ref

    shape = replace(ScaleShape(), tables=300, columns_per_table=4, tables_per_schema=100)
    store = _store(tmp_path / "w.db", 300)
    guru = Guru(store=store)
    child, parent = table_ref(57, shape), table_ref(203, shape)
    try:
        assert parent.table not in str(guru.context(child.table))  # graph and index built
        with SQLiteStore(tmp_path / "w.db") as other:
            other.put_facts([_new_join(child, parent)])
        view = str(guru.context(f"{child.table} updated"))
        assert f"{parent.schema}.{parent.table}.id" in view
    finally:
        guru.close()
        store.close()


def test_a_join_the_background_drainer_measures_reaches_the_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The in-process path: a background drain that writes a JOIN invalidates this ``Guru``
    (its scheduler's ``on_write``), and the store's JOIN counter moves too."""
    from benchmarks.scale.generator import table_ref

    from schemaguru.config import Config
    from schemaguru.measure.scheduler import BackgroundDrainer, MeasureReport, Scheduler

    shape = replace(ScaleShape(), tables=300, columns_per_table=4, tables_per_schema=100)
    store = _store(tmp_path / "d.db", 300)
    guru = Guru(store=store, config=Config(measure_on_demand="background"))
    child, parent = table_ref(57, shape), table_ref(203, shape)

    def measured(self: Scheduler, *args: Any, **kwargs: Any) -> MeasureReport:
        self.store.put_facts([_new_join(child, parent)])
        return MeasureReport()

    try:
        assert parent.table not in str(guru.context(child.table))
        monkeypatch.setattr(Scheduler, "run", measured)
        BackgroundDrainer(guru._scheduler).run_once()  # what the drainer thread runs
        assert f"{parent.schema}.{parent.table}.id" in str(guru.context(f"{child.table} updated"))
    finally:
        guru.close()
        store.close()
