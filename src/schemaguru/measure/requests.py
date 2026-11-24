"""SchemaGuru-level measurement requests (spec 8.1).

A request says *what* to measure; a :class:`~schemaguru.measure.backend.MeasureBackend` turns it
into FactLang IR and runs it. SchemaGuru never writes measurement SQL (spec 5.2). Every request
is frozen and hashable and carries ``sample``: ``None`` means exact, a :class:`SampleChoice`
means a representative sample chosen by :func:`schemaguru.measure.sampling.choose_sample`.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Union

from schemaguru.model.joins import JoinRef
from schemaguru.model.refs import ObjectRef

SampleMethod = Literal["keyhash", "system", "bernoulli"]
_METHODS = ("keyhash", "system", "bernoulli")


def format_fraction(fraction: Decimal) -> str:
    """Plain decimal text without exponent or trailing zeros (``Decimal("0.010")`` -> ``0.01``)."""
    text = format(fraction.normalize(), "f")
    return text


@dataclass(frozen=True)
class SampleChoice:
    """How one request samples its input (FactLang ``SampleSpec``, Amendment B).

    ``key`` names the hashed columns for ``keyhash`` (left side for a join; the bridge uses the
    right side's own columns with the same fraction and seed).
    """

    method: SampleMethod
    fraction: Decimal
    seed: int
    key: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.method not in _METHODS:
            raise ValueError(f"unknown sample method {self.method!r}")
        if not isinstance(self.fraction, Decimal) or not Decimal(0) < self.fraction <= 1:
            raise ValueError(f"sample fraction must be a Decimal in (0, 1], got {self.fraction!r}")
        if self.method == "keyhash" and not self.key:
            raise ValueError("keyhash sampling needs the key columns")
        if self.method != "keyhash" and self.key:
            raise ValueError(f"{self.method} sampling takes no key")

    def method_string(self) -> str:
        """``sampled:<method>:<fraction>:<seed>`` (spec 8.6)."""
        return f"sampled:{self.method}:{format_fraction(self.fraction)}:{self.seed}"


def _column(ref: ObjectRef) -> None:
    if ref.column is None:
        raise ValueError(f"a column ref is required, got {ref.canonical()}")


def _table(ref: ObjectRef) -> None:
    if ref.column is not None:
        raise ValueError(f"a table ref is required, got {ref.canonical()}")


def _columns(cols: tuple[str, ...], *, allow_empty: bool = False) -> None:
    if not cols and not allow_empty:
        raise ValueError("at least one column is required")
    if len(set(cols)) != len(cols):
        raise ValueError(f"duplicate column in {cols!r}")


@dataclass(frozen=True)
class ProfileColumns:
    """Row count plus non-null and distinct counts of each column (fused per table)."""

    table: ObjectRef
    columns: tuple[str, ...]
    sample: SampleChoice | None = None

    def __post_init__(self) -> None:
        _table(self.table)
        _columns(self.columns, allow_empty=True)

    @property
    def table_ref(self) -> ObjectRef:
        return self.table


@dataclass(frozen=True)
class TopValues:
    """The exact top-``k`` observed values with counts (``ValueMembership(declared=())``)."""

    column: ObjectRef
    k: int
    sample: SampleChoice | None = None

    def __post_init__(self) -> None:
        _column(self.column)
        if not 1 <= self.k <= 1000:
            raise ValueError(f"k must be 1..1000, got {self.k}")

    @property
    def table_ref(self) -> ObjectRef:
        return self.column.table_ref()


@dataclass(frozen=True)
class KeyOverlapReq:
    """Both directions of a join in one probe: match, coverage, row match, multiplicity."""

    join: JoinRef
    multiplicity: bool = True
    sample: SampleChoice | None = None

    @property
    def table_ref(self) -> ObjectRef:
        return self.join.left[0].table_ref()


@dataclass(frozen=True)
class Uniqueness:
    """Key uniqueness of a (possibly composite) column tuple, grouped in full."""

    table: ObjectRef
    columns: tuple[str, ...]
    sample: SampleChoice | None = None

    def __post_init__(self) -> None:
        _table(self.table)
        _columns(self.columns)

    @property
    def table_ref(self) -> ObjectRef:
        return self.table


@dataclass(frozen=True)
class Freshness:
    """``MAX`` of a timestamp-like column. Never sampled (spec 8.6)."""

    column: ObjectRef
    sample: SampleChoice | None = None

    def __post_init__(self) -> None:
        _column(self.column)
        if self.sample is not None:
            raise ValueError("freshness is never sampled")

    @property
    def table_ref(self) -> ObjectRef:
        return self.column.table_ref()


@dataclass(frozen=True)
class PatternRate:
    """Share of non-null values matching a case-folded ``LIKE`` pattern (``TYPE_NOTE``)."""

    column: ObjectRef
    pattern: str
    means: str | None = None
    sample: SampleChoice | None = None

    def __post_init__(self) -> None:
        _column(self.column)
        if not self.pattern.strip("%_"):
            raise ValueError(f"pattern {self.pattern!r} would match every value")

    @property
    def table_ref(self) -> ObjectRef:
        return self.column.table_ref()


@dataclass(frozen=True)
class RowCount:
    """Exact ``COUNT(*)``. Never sampled (spec 8.6)."""

    table: ObjectRef
    sample: SampleChoice | None = None

    def __post_init__(self) -> None:
        _table(self.table)
        if self.sample is not None:
            raise ValueError("row counts are never sampled")

    @property
    def table_ref(self) -> ObjectRef:
        return self.table


Request = Union[  # noqa: UP007 - a runtime alias, usable on Python 3.10
    ProfileColumns, TopValues, KeyOverlapReq, Uniqueness, Freshness, PatternRate, RowCount
]
REQUEST_TYPES: tuple[type, ...] = (
    ProfileColumns,
    TopValues,
    KeyOverlapReq,
    Uniqueness,
    Freshness,
    PatternRate,
    RowCount,
)
NEVER_SAMPLED: tuple[type, ...] = (Freshness, RowCount)
