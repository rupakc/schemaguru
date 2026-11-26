"""Definition analysis: worker results to UNVERIFIED lineage facts (0.2.0 spec 4.1, 4.4)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime, timezone

import pytest

pytest.importorskip("sqlglot")

from schemaguru.diagnostics import CODES, Severity
from schemaguru.lineage.analyse import (
    DefinitionItem,
    DefinitionResult,
    analyse_definitions,
    extra_missing_diagnostic,
    failure_diagnostic,
    lineage_facts,
)
from schemaguru.lineage.parse import ColumnLineage, ParsedLineage
from schemaguru.lineage.worker import ParserPool
from schemaguru.model.facts import Fact, Provenance, Status
from schemaguru.model.lineage import RawName, ResolvedTable
from schemaguru.model.refs import ObjectRef
from schemaguru.pii.mask import definition_template

VIEW = ObjectRef(None, "shop", "big_orders")
ORDERS = ResolvedTable("shop.orders", ("id", "amount", "note"))
SQL = (
    "SELECT o.id, o.amount, CAST(o.amount AS INT) AS amount_int "
    "FROM shop.orders AS o, legacy.archive AS a "
    "WHERE o.note = 'alice@example.com' AND o.amount > 73914286501"
)
AT = datetime(2026, 11, 25, tzinfo=timezone.utc)


def _resolve(names: Sequence[RawName], target: ObjectRef) -> Mapping[RawName, ResolvedTable]:
    assert target == VIEW
    return {n: ORDERS for n in names if n == (None, "shop", "orders")}


@pytest.fixture(scope="module")
def pool() -> Iterator[ParserPool]:
    with ParserPool() as p:
        yield p


def _result(pool: ParserPool, sql: str = SQL, dialect: str = "postgres") -> DefinitionResult:
    (res,) = analyse_definitions(
        [DefinitionItem(VIEW, sql, dialect)],
        resolve=_resolve,
        pool=pool,
        template=definition_template,
    )
    return res


def _facts(
    res: DefinitionResult, *, evidence: bool = False, cols: Sequence[str] = ()
) -> list[Fact]:
    return lineage_facts(
        res,
        target_columns=cols or ("id", "amount", "amount_int"),
        provenance=Provenance.CATALOG,
        source="view_definition",
        observed_at=AT,
        evidence=evidence,
    )


def test_facts_from_result(pool: ParserPool) -> None:
    res = _result(pool)
    assert res.failure is None and res.parsed is not None
    assert res.sha256 == hashlib.sha256(SQL.encode("utf-8")).hexdigest()
    facts = _facts(res)
    assert [(f.kind.name, f.subject.canonical()) for f in facts] == [  # type: ignore[union-attr]
        ("LINEAGE", "shop.big_orders"),
        ("COLUMN_LINEAGE", "shop.big_orders.amount"),
        ("COLUMN_LINEAGE", "shop.big_orders.amount_int"),
        ("COLUMN_LINEAGE", "shop.big_orders.id"),
    ]
    assert all(f.status is Status.UNVERIFIED for f in facts)
    assert facts[0].value == {
        "upstream": ["shop.orders"],
        "unresolved": ["legacy.archive"],
        "ignore": False,
    }
    assert facts[2].value["transform"] == "cast"
    assert facts[0].evidence is None
    # ids are deterministic: a second analysis in a fresh worker yields the same facts
    with ParserPool() as other:
        assert [f.id for f in _facts(_result(other))] == [f.id for f in facts]


def test_evidence_is_template_only(pool: ParserPool) -> None:
    res = _result(pool)
    facts = _facts(res, evidence=True)
    assert facts[0].evidence is not None
    assert facts[0].evidence.sql == res.template
    assert facts[0].evidence.dialect == "postgres"
    assert all(f.evidence is None for f in facts[1:])
    dumped = json.dumps([f.to_dict() for f in facts])
    for literal in ("alice@example.com", "73914286501", "alice"):
        assert literal not in dumped
    assert "shop.orders" in dumped  # negative control: the dump does carry the definition


def test_no_template_no_evidence(pool: ParserPool) -> None:
    (res,) = analyse_definitions(
        [DefinitionItem(VIEW, SQL, "postgres")],
        resolve=_resolve,
        pool=pool,
        template=lambda _sql: None,
    )
    assert res.template is None
    assert all(f.evidence is None for f in _facts(res, evidence=True))


def test_failure_result_has_no_facts(pool: ParserPool) -> None:
    res = _result(pool, sql="SELECT FROM (")
    assert res.failure == "parse_error" and res.parsed is None and res.template is None
    assert _facts(res) == []
    assert _result(pool, dialect="nope").failure == "unsupported_dialect"


def test_results_in_input_order(pool: ParserPool) -> None:
    items = [
        DefinitionItem(VIEW, "SELECT id FROM shop.orders", "postgres"),
        DefinitionItem(VIEW, "SELECT FROM (", "postgres"),
        DefinitionItem(VIEW, "SELECT amount FROM shop.orders", "postgres"),
    ]
    out = analyse_definitions(items, resolve=_resolve, pool=pool, template=definition_template)
    assert [r.failure for r in out] == [None, "parse_error", None]
    assert [r.parsed.columns[0].column if r.parsed else None for r in out] == [
        "id",
        None,
        "amount",
    ]


def test_failure_diagnostic_text_fixed() -> None:
    d = failure_diagnostic(VIEW, "timeout")
    assert d.code == "SG211" and d.severity is Severity.WARNING
    assert d.message == "lineage could not be parsed for shop.big_orders: timeout"
    assert d.location == "shop.big_orders"
    e = extra_missing_diagnostic()
    assert e.code == "SG212" and e.severity is Severity.INFO
    assert e.message == (
        "column-level lineage needs the `lineage` extra (pip install 'schemaguru[lineage]')"
    )
    assert "SG211" in CODES and "SG212" in CODES


def _parsed(*cols: str) -> DefinitionResult:
    return DefinitionResult(
        target=VIEW,
        dialect="postgres",
        sha256="0" * 64,
        parsed=ParsedLineage(
            upstream=("shop.orders",),
            unresolved=(),
            columns=tuple(ColumnLineage(c, ("shop.orders.id",), "copy", ()) for c in cols),
        ),
        failure=None,
        template=None,
    )


def test_column_match_case_insensitive_unique() -> None:
    subjects = lambda facts: [f.subject.column for f in facts[1:]]  # noqa: E731
    # exact, then a unique case-insensitive match
    assert subjects(_facts(_parsed("Id", "amount"), cols=("id", "amount"))) == ["id", "amount"]
    # ambiguous case-insensitively: skipped
    assert subjects(_facts(_parsed("ID"), cols=("id", "Id"))) == []
    # exact wins over the case-insensitive candidate, and a target column is used once
    assert subjects(_facts(_parsed("id", "ID"), cols=("id",))) == ["id"]
    # a parsed column with no target column is skipped
    assert subjects(_facts(_parsed("ghost"), cols=("id",))) == []


def test_no_table_fact_when_nothing_upstream() -> None:
    res = DefinitionResult(VIEW, "postgres", "0" * 64, ParsedLineage((), (), ()), None, None)
    assert _facts(res) == []
