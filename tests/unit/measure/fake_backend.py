"""A ``MeasureBackend`` fake for unit tests.

Choice (documented per the Task 6 ruling): payloads are *computed* by running SQL against the
shop DuckDB connection, so every number is consistent with ``ShopFacts`` by construction.
This SQL lives in tests only; ``src/`` never writes measurement SQL (spec 5.2).

Sampled payloads look like FactLang Amendment B3: raw metrics on the sampled rows plus
``sampled_rows`` (``sampled_left_rows``/``sampled_right_rows`` for a join). ``keyhash`` keeps a
row iff ``hash('<seed>|' || key) % 1000000 < fraction * 1000000``, so both sides of a join keep
the same key values. ``system`` is rendered as DuckDB row-level ``bernoulli`` with the seed:
DuckDB's block sampling works in 2048-row vectors, too coarse for the tiny fixture tables.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from schemaguru.diagnostics import Diagnostic, Severity
from schemaguru.measure.backend import CostEstimate, MeasureResult
from schemaguru.measure.requests import (
    Freshness,
    KeyOverlapReq,
    PatternRate,
    ProfileColumns,
    Request,
    RowCount,
    SampleChoice,
    TopValues,
    Uniqueness,
)
from schemaguru.model.evidence import Evidence
from schemaguru.model.refs import ObjectRef

BYTES_PER_CELL = 8


def q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def tbl(ref: ObjectRef) -> str:
    return ".".join(q(p) for p in (ref.catalog, ref.schema, ref.table) if p is not None)


def _source(table: ObjectRef, sample: SampleChoice | None, key: Sequence[str] = ()) -> str:
    """A FROM item: the table, or a sampled subquery of it."""
    if sample is None:
        return tbl(table)
    if sample.method == "keyhash":
        cols = key or sample.key
        parts = " || '|' || ".join(f"CAST({q(c)} AS VARCHAR)" for c in cols)
        not_null = " AND ".join(f"{q(c)} IS NOT NULL" for c in cols)
        bound = int((sample.fraction * 1_000_000).to_integral_value(rounding=ROUND_HALF_UP))
        return (
            f"(SELECT * FROM {tbl(table)} WHERE {not_null} AND "
            f"hash('{sample.seed}' || '|' || {parts}) % 1000000 < {bound})"
        )
    pct = float(sample.fraction * 100)
    seed = sample.seed % (2**31)
    return f"(SELECT * FROM {tbl(table)} USING SAMPLE {pct}% (bernoulli, {seed}))"


@dataclass
class FakeBackend:
    conn: Any
    fail: Callable[[Request], bool] = lambda req: False
    cost: Callable[[Request], int | None] | None = None
    caps: frozenset[str] = frozenset({"measure", "estimate", "sample", "composite_join"})
    measured: list[list[Request]] = field(default_factory=list)
    estimated: list[Request] = field(default_factory=list)

    # --- protocol -------------------------------------------------------------------
    def capabilities(self) -> frozenset[str]:
        return self.caps

    def check(self, claims: str) -> list[Evidence]:
        return []

    def estimate(self, reqs: Sequence[Request]) -> list[CostEstimate]:
        out: list[CostEstimate] = []
        for r in reqs:
            self.estimated.append(r)
            b = self.cost(r) if self.cost is not None else self._default_cost(r)
            out.append(CostEstimate(r, b, "unknown" if b is None else "dry_run"))
        return out

    def measure(self, reqs: Sequence[Request]) -> list[MeasureResult]:
        self.measured.append(list(reqs))
        out: list[MeasureResult] = []
        for r in reqs:
            if self.fail(r):
                out.append(
                    MeasureResult(
                        r,
                        None,
                        None,
                        Diagnostic(
                            "SG303", Severity.ERROR, f"injected failure for {type(r).__name__}"
                        ),
                    )
                )
                continue
            try:
                payload, sql = self._run(r)
            except Exception as e:  # the fake mirrors FactLang: execution errors never raise
                out.append(
                    MeasureResult(r, None, None, Diagnostic("SG303", Severity.ERROR, str(e)))
                )
                continue
            sample = (
                None
                if r.sample is None
                else {
                    "method": r.sample.method,
                    "fraction": str(r.sample.fraction),
                    "seed": r.sample.seed,
                    "key": list(r.sample.key),
                }
            )
            ev = Evidence(
                sql=sql,
                dialect="duckdb",
                sample=sample,
                raw={"method": "exact" if r.sample is None else r.sample.method_string()},
            )
            out.append(MeasureResult(r, payload, ev, None))
        return out

    # --- helpers --------------------------------------------------------------------
    def _one(self, sql: str) -> dict[str, Any]:
        cur = self.conn.execute(sql)
        names = [d[0] for d in cur.description]
        row = cur.fetchone()
        return dict(zip(names, row, strict=True))

    def _rows(self, table: ObjectRef) -> int:
        return int(self.conn.execute(f"SELECT COUNT(*) FROM {tbl(table)}").fetchone()[0])

    def _default_cost(self, r: Request) -> int | None:
        if isinstance(r, KeyOverlapReq):
            left = r.join.left[0].table_ref()
            right = r.join.right[0].table_ref()
            cells = self._rows(left) * len(r.join.left) + self._rows(right) * len(r.join.right)
            return cells * BYTES_PER_CELL
        ncols = len(r.columns) if isinstance(r, ProfileColumns | Uniqueness) else 1
        b = self._rows(r.table_ref) * max(1, ncols) * BYTES_PER_CELL
        if r.sample is not None and r.sample.method == "system":
            b = int(Decimal(b) * r.sample.fraction)
        return b

    def _run(self, r: Request) -> tuple[dict[str, Any] | None, str]:
        n_sampled = "COUNT(*) AS sampled_rows" if r.sample is not None else "1 AS _x"
        if isinstance(r, ProfileColumns):
            parts = ["COUNT(*) AS row_count", n_sampled] + [
                f"COUNT({q(c)}) AS nn_{i}, COUNT(DISTINCT {q(c)}) AS d_{i}"
                for i, c in enumerate(r.columns)
            ]
            sql = f"SELECT {', '.join(parts)} FROM {_source(r.table, r.sample)} s"
            row = self._one(sql)
            payload: dict[str, Any] = {
                "row_count": row["row_count"],
                "columns": {
                    c: {"non_null": row[f"nn_{i}"], "distinct_keys": row[f"d_{i}"]}
                    for i, c in enumerate(r.columns)
                },
            }
            if r.sample is not None:
                payload["sampled_rows"] = row["sampled_rows"]
            return payload, sql
        if isinstance(r, TopValues):
            c = q(r.column.column or "")
            src = _source(r.column.table_ref(), r.sample)
            head = self._one(
                f"SELECT COUNT({c}) AS non_null, COUNT(DISTINCT {c}) AS distinct_values, "
                f"{n_sampled} FROM {src} s"
            )
            sql = (
                f"SELECT {c} AS value, COUNT(*) AS cnt FROM {src} s WHERE {c} IS NOT NULL "
                f"GROUP BY 1 ORDER BY cnt DESC, value ASC LIMIT {r.k}"
            )
            vals = [{"value": v, "count": n} for v, n in self.conn.execute(sql).fetchall()]
            payload = {
                "non_null": head["non_null"],
                "distinct": head["distinct_values"],
                "values": vals,
            }
            if r.sample is not None:
                payload["sampled_rows"] = head["sampled_rows"]
            return payload, sql
        if isinstance(r, KeyOverlapReq):
            return self._overlap(r)
        if isinstance(r, Uniqueness):
            cols = ", ".join(q(c) for c in r.columns)
            nn = " AND ".join(f"{q(c)} IS NOT NULL" for c in r.columns)
            src = _source(r.table, r.sample, r.columns)
            sql = (
                f"WITH s AS (SELECT * FROM {src} x), g AS (SELECT {cols}, COUNT(*) AS n FROM s "
                f"WHERE {nn} GROUP BY {cols}) SELECT (SELECT COUNT(*) FROM s) AS row_count, "
                f"(SELECT COUNT(*) FROM s WHERE {nn}) AS non_null, "
                f"(SELECT COUNT(*) FROM g) AS distinct_keys, "
                f"(SELECT MAX(n) FROM g) AS max_rows_per_key, "
                f"(SELECT COUNT(*) FROM g WHERE n > 1) AS duplicate_keys"
            )
            payload = self._one(sql)
            if r.sample is not None:
                payload["sampled_rows"] = payload["row_count"]
            return payload, sql
        if isinstance(r, Freshness):
            sql = f"SELECT MAX({q(r.column.column or '')}) AS max_value FROM {tbl(r.table_ref)}"
            return self._one(sql), sql
        if isinstance(r, PatternRate):
            c = q(r.column.column or "")
            pat = r.pattern.replace("'", "''")
            sql = (
                f"SELECT COUNT({c}) AS non_null, "
                f"COUNT(*) FILTER (WHERE lower({c}) LIKE lower('{pat}')) AS matching, "
                f"{n_sampled} FROM {_source(r.column.table_ref(), r.sample)} s"
            )
            payload = self._one(sql)
            payload.pop("_x", None)
            return payload, sql
        assert isinstance(r, RowCount)
        sql = f"SELECT COUNT(*) AS row_count FROM {tbl(r.table)}"
        return self._one(sql), sql

    def _overlap(self, r: KeyOverlapReq) -> tuple[dict[str, Any], str]:
        j = r.join

        def side(cols: Sequence[ObjectRef], sample: SampleChoice | None) -> str:
            names = [c.column or "" for c in cols]
            exprs = ", ".join(
                f"CAST({q(n)} AS VARCHAR) AS k{i}" if j.cast_to == "string" else f"{q(n)} AS k{i}"
                for i, n in enumerate(names)
            )
            nn = " AND ".join(f"{q(n)} IS NOT NULL" for n in names)
            right_sample = sample
            if sample is not None and sample.method == "keyhash":
                right_sample = SampleChoice("keyhash", sample.fraction, sample.seed, tuple(names))
            return (
                f"SELECT {exprs} FROM {_source(cols[0].table_ref(), right_sample, names)} x "
                f"WHERE {nn}"
            )

        keys = ", ".join(f"k{i}" for i in range(len(j.left)))
        on = " AND ".join(f"l.k{i} = r.k{i}" for i in range(len(j.left)))
        sql = (
            f"WITH l AS ({side(j.left, r.sample)}), r AS ({side(j.right, r.sample)}), "
            f"lk AS (SELECT {keys}, COUNT(*) AS n FROM l GROUP BY {keys}), "
            f"rk AS (SELECT {keys}, COUNT(*) AS n FROM r GROUP BY {keys}) "
            f"SELECT (SELECT COUNT(*) FROM lk) AS distinct_left, "
            f"(SELECT COUNT(*) FROM lk l WHERE EXISTS (SELECT 1 FROM rk r WHERE {on})) AS matched, "
            f"(SELECT COUNT(*) FROM rk) AS distinct_right, "
            f"(SELECT COUNT(*) FROM l) AS left_non_null, "
            f"(SELECT COALESCE(SUM(n), 0) FROM lk l WHERE EXISTS (SELECT 1 FROM rk r WHERE {on})) "
            f"AS matched_left_rows, (SELECT MAX(n) FROM lk) AS max_left_per_key, "
            f"(SELECT MAX(n) FROM rk) AS max_right_per_key"
        )
        p = self._one(sql)
        if not r.multiplicity:
            p.pop("max_left_per_key")
            p.pop("max_right_per_key")
        if r.sample is not None:
            p["sampled_left_rows"] = p["left_non_null"]
            p["sampled_right_rows"] = self._one(
                f"SELECT COUNT(*) AS n FROM ({side(j.right, r.sample)})"
            )["n"]
        return p, sql
