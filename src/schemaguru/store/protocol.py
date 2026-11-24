"""The ``Store`` protocol (spec 7.1): CRUD over the model plus search, snapshots and state."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Literal, Protocol

from schemaguru.model.facts import Fact
from schemaguru.model.joins import JoinRef
from schemaguru.model.refs import ObjectRef
from schemaguru.model.structure import Column, Constraint, Schema, Table

LedgerUnit = Literal["bytes", "tokens", "queries"]


class Store(Protocol):
    """All writes are idempotent upserts keyed on stable ids."""

    def upsert_schemas(self, items: Iterable[Schema]) -> None: ...

    def upsert_tables(self, items: Iterable[Table]) -> None:
        """Insert or replace; a table written with ``withdrawn_at=None`` is live again."""
        ...

    def upsert_columns(self, table: ObjectRef, cols: Sequence[Column]) -> None:
        """The given list becomes the table's complete column list."""
        ...

    def upsert_constraints(self, table: ObjectRef, cons: Sequence[Constraint]) -> None:
        """The given list becomes the table's complete constraint list."""
        ...

    def mark_withdrawn(self, refs: Iterable[ObjectRef], at: datetime) -> None:
        """Soft-delete; an earlier ``withdrawn_at`` is kept. Facts and overlays are untouched."""
        ...

    def table(self, ref: ObjectRef) -> Table | None: ...

    def columns(self, ref: ObjectRef) -> list[Column]: ...

    def constraints(self, ref: ObjectRef) -> list[Constraint]: ...

    def iter_schemas(self) -> Iterator[Schema]: ...

    def iter_tables(self, *, include_withdrawn: bool = False) -> Iterator[Table]:
        """Streaming, ordered by canonical ref; never loads the whole catalog."""
        ...

    def resolve(self, text: str) -> tuple[ObjectRef | None, list[ObjectRef]]:
        """Resolve user input to a stored ref: exact first, then a unique case-insensitive one.

        Table refs are resolved by default (see ``ObjectRef.parse``: four parts are a column);
        a ``table.column`` form is tried as a table first, then with ``has_column=True``.
        Returns ``(ref, [])`` on success, ``(None, candidates)`` when ambiguous (never a guess),
        and ``(None, [])`` when nothing matches or the text is not a valid ref.
        """
        ...

    def put_facts(self, facts: Iterable[Fact]) -> None:
        """Upsert by ``Fact.id``: re-measurement replaces the stored row."""
        ...

    def delete_facts(self, ids: Iterable[str]) -> int:
        """Remove facts from the live knowledge by id (snapshots keep their copies); returns
        how many existed. Used to retract HUMAN facts whose overlay entry was removed."""
        ...

    def iter_facts(self) -> Iterator[Fact]: ...

    def facts_for(
        self,
        subjects: Iterable[ObjectRef | JoinRef] | None,
        kinds: Iterable[str] | None,
    ) -> list[Fact]:
        """``None`` means no filter; an empty iterable matches nothing. Sorted by id."""
        ...

    def search(self, query: str, k: int) -> list[tuple[ObjectRef, float]]:
        """Lexical search over live tables; higher score is better; name hits rank first."""
        ...

    def tables_named(self, names: Iterable[str], *, limit: int) -> list[ObjectRef]:
        """Live tables whose bare table name equals one of ``names``, case-insensitively.

        Ordered by canonical ref, at most ``limit``.
        """
        ...

    def snapshot(self, label: str | None = None, *, checks_run: Iterable[str] = ()) -> str: ...

    def snapshot_facts(self, snapshot_id: str) -> list[Fact]: ...

    def snapshot_checks_run(self, snapshot_id: str) -> frozenset[str]: ...

    def latest_snapshot(self) -> str | None: ...

    def crawl_state_get(self, key: str) -> Mapping[str, object] | None: ...

    def crawl_state_set(self, key: str, value: Mapping[str, object]) -> None: ...

    def ledger_add(self, kind: str, amount: int, unit: LedgerUnit, note: str) -> None: ...

    def ledger_total(self, kind: str, unit: LedgerUnit, since: datetime | None) -> int: ...

    def queue_push(self, table: ObjectRef, reason: str, priority: float) -> None: ...

    def queue_pop(self, n: int) -> list[tuple[ObjectRef, str]]: ...

    def feedback_add(self, table: ObjectRef, column: str | None, failure_class: str) -> None: ...

    def feedback_summary(self) -> list[dict[str, object]]: ...

    def transaction(self) -> AbstractContextManager[None]: ...
