"""Measurement results -> facts (spec 4.3-4.5, 8.1, 8.5, 8.6).

* exact -> ``OBSERVED`` with ``method="exact"``; sampled -> ``ESTIMATED`` with the
  ``sampled:<method>:<fraction>:<seed>`` method and ``value["estimate"]`` (an
  :class:`EstimatedValue` as a dict). Sampled numbers are never ``VERIFIED``.
* a probe error -> one ``ERROR`` fact per subject the probe would have produced.
* an empty table -> ``UNMEASURABLE``, distinct from an all-null column (``NULL_RATE`` 1.0,
  ``dead``).
* unknown is ``None``, never 0 or 1.0; counts are ``int``, rates ``float``.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from schemaguru.config import Config
from schemaguru.measure.backend import MeasureResult
from schemaguru.measure.estimates import EstimatedValue, scale_count, wilson
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
from schemaguru.model.facts import Fact, Provenance, Status
from schemaguru.model.joins import JOIN_VALUE_KEYS, JoinRef, join_verdict
from schemaguru.model.kinds import FactKind, KindSpec
from schemaguru.model.refs import ObjectRef

SOURCE = "measure"
WILSON = "wilson95"
PATTERN_HOLDS = 0.99
_REASONS = {"SG301": "over_budget", "SG303": "probe_error", "SG304": "capability_missing"}


def _int(v: Any) -> int | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, Decimal | float):
        return int(v)
    return None


def _ratio(num: int | None, den: int | None) -> float | None:
    if num is None or den is None or den <= 0:
        return None
    return num / den


def _plain(v: Any) -> Any:
    """A JSON-safe scalar for stored values (Decimal and dates become text)."""
    if v is None or isinstance(v, bool | int | float | str):
        return v
    if isinstance(v, datetime):
        aware = v if v.tzinfo is not None else v.replace(tzinfo=timezone.utc)
        return aware.astimezone(timezone.utc).isoformat()
    if isinstance(v, date | Decimal):
        return str(v)
    return str(v)


def _targets(req: Request) -> list[tuple[KindSpec, ObjectRef | JoinRef, dict[str, Any]]]:
    """Every (kind, subject, identity payload) a request produces; used for ERROR facts."""
    if isinstance(req, ProfileColumns):
        out: list[tuple[KindSpec, ObjectRef | JoinRef, dict[str, Any]]] = []
        if req.sample is None:
            out.append((FactKind.ROW_COUNT, req.table, {}))
        for c in req.columns:
            col = ObjectRef(req.table.catalog, req.table.schema, req.table.table, c)
            out += [(FactKind.NULL_RATE, col, {}), (FactKind.DISTINCT, col, {})]
        return out
    if isinstance(req, TopValues):
        return [(FactKind.VALUES, req.column, {"case_sensitive": True, "split": None})]
    if isinstance(req, KeyOverlapReq):
        return [(FactKind.JOIN, req.join, {})]
    if isinstance(req, Uniqueness):
        return [(FactKind.PRIMARY_KEY, req.table, {"columns": list(req.columns)})]
    if isinstance(req, Freshness):
        return [(FactKind.FRESHNESS, req.column.table_ref(), {"column": req.column.column})]
    if isinstance(req, PatternRate):
        return [(FactKind.TYPE_NOTE, req.column, _type_note_identity(req))]
    return [(FactKind.ROW_COUNT, req.table, {})]


def _type_note_identity(req: PatternRate) -> dict[str, Any]:
    return {"stored_as": "string", "means": req.means or f"LIKE {req.pattern}"}


class _Ctx:
    def __init__(
        self, result: MeasureResult, observed_at: datetime, snapshot_id: str, config: Config
    ) -> None:
        self.req = result.request
        self.sample: SampleChoice | None = result.request.sample
        self.method = "exact" if self.sample is None else self.sample.method_string()
        self.evidence = result.evidence
        self.observed_at = observed_at
        self.snapshot_id = snapshot_id
        self.config = config
        self.deff = (
            config.sampling.design_effect
            if self.sample is not None and self.sample.method == "system"
            else 1.0
        )

    def fact(
        self,
        kind: KindSpec,
        subject: ObjectRef | JoinRef,
        value: Mapping[str, Any],
        status: Status | None = None,
    ) -> Fact:
        if status is None:
            status = Status.OBSERVED if self.sample is None else Status.ESTIMATED
        return Fact.make(
            kind,
            subject,
            dict(value),
            Provenance.MEASURED,
            status,
            method=self.method,
            evidence=self.evidence,
            source=SOURCE,
            observed_at=self.observed_at,
            snapshot_id=self.snapshot_id,
        )

    def sample_info(self, n: int | None) -> dict[str, Any]:
        if self.sample is None:
            return {}
        from schemaguru.measure.requests import format_fraction

        return {"sampled_rows": n, "fraction": format_fraction(self.sample.fraction)}

    def rate_estimate(self, successes: int, n: int, deff: float | None = None) -> EstimatedValue:
        ci = wilson(successes, n, design_effect=self.deff if deff is None else deff)
        lo, hi = (None, None) if ci is None else ci
        return EstimatedValue(_ratio(successes, n), lo, hi, WILSON, False)


def to_facts(
    result: MeasureResult, *, observed_at: datetime, snapshot_id: str, config: Config
) -> list[Fact]:
    ctx = _Ctx(result, observed_at, snapshot_id, config)
    req = result.request
    if result.error is not None:
        code = result.error.code
        reason = _REASONS.get(code, "probe_error")
        return [
            ctx.fact(
                k,
                s,
                {**ident, "reason": reason, "code": code, "message": result.error.message},
                Status.ERROR,
            )
            for k, s, ident in _targets(req)
        ]
    p = result.payload
    if p is None:
        return [
            ctx.fact(k, s, {**ident, "reason": "no_data"}, Status.UNMEASURABLE)
            for k, s, ident in _targets(req)
        ]
    if isinstance(req, ProfileColumns):
        return _profile(ctx, req, p)
    if isinstance(req, TopValues):
        return _top_values(ctx, req, p)
    if isinstance(req, KeyOverlapReq):
        return [_join(ctx, req, p)]
    if isinstance(req, Uniqueness):
        return [_uniqueness(ctx, req, p)]
    if isinstance(req, Freshness):
        return [_freshness(ctx, req, p)]
    if isinstance(req, PatternRate):
        return _pattern(ctx, req, p)
    assert isinstance(req, RowCount)
    rows = _int(p.get("row_count"))
    if rows is None:
        return [ctx.fact(FactKind.ROW_COUNT, req.table, {"rows": None}, Status.UNMEASURABLE)]
    return [ctx.fact(FactKind.ROW_COUNT, req.table, {"rows": rows})]


def _profile(ctx: _Ctx, req: ProfileColumns, p: Mapping[str, Any]) -> list[Fact]:
    rows = _int(p.get("row_count"))
    n = _int(p.get("sampled_rows", rows)) if ctx.sample is not None else rows
    out: list[Fact] = []
    if ctx.sample is None:
        out.append(
            ctx.fact(
                FactKind.ROW_COUNT,
                req.table,
                {"rows": rows},
                None if rows is not None else Status.UNMEASURABLE,
            )
        )
    cols: Mapping[str, Any] = p.get("columns") or {}
    for c in req.columns:
        col = ObjectRef(req.table.catalog, req.table.schema, req.table.table, c)
        metrics: Mapping[str, Any] = cols.get(c) or {}
        non_null = _int(metrics.get("non_null"))
        distinct = _int(metrics.get("distinct_keys"))
        if n is None or n == 0:
            reason = "empty_table" if ctx.sample is None else "empty_sample"
            if n is None:
                reason = "missing_metric"
            out.append(
                ctx.fact(
                    FactKind.NULL_RATE,
                    col,
                    {"rate": None, "dead": None, "reason": reason},
                    Status.UNMEASURABLE,
                )
            )
            out.append(
                ctx.fact(
                    FactKind.DISTINCT,
                    col,
                    {"distinct_keys": None, "reason": reason},
                    Status.UNMEASURABLE,
                )
            )
            continue
        if non_null is None or non_null > n:
            out.append(
                ctx.fact(
                    FactKind.NULL_RATE,
                    col,
                    {"rate": None, "dead": None, "reason": "missing_metric"},
                    Status.UNMEASURABLE,
                )
            )
        elif ctx.sample is None:
            out.append(
                ctx.fact(
                    FactKind.NULL_RATE,
                    col,
                    {
                        "rate": (n - non_null) / n,
                        "dead": non_null == 0,
                        "rows": n,
                        "non_null": non_null,
                    },
                )
            )
        else:
            est = ctx.rate_estimate(n - non_null, n)
            out.append(
                ctx.fact(
                    FactKind.NULL_RATE,
                    col,
                    {
                        "rate": est.value,
                        "dead": False if non_null > 0 else None,
                        "estimate": est.__dict__,
                        **ctx.sample_info(n),
                    },
                )
            )
        if distinct is None:
            out.append(
                ctx.fact(
                    FactKind.DISTINCT,
                    col,
                    {"distinct_keys": None, "reason": "missing_metric"},
                    Status.UNMEASURABLE,
                )
            )
        elif ctx.sample is None:
            out.append(ctx.fact(FactKind.DISTINCT, col, {"distinct_keys": distinct}))
        else:
            dv = _sampled_distinct(ctx, distinct, (c,))
            out.append(
                ctx.fact(
                    FactKind.DISTINCT,
                    col,
                    {
                        "distinct_keys": dv.value,
                        "estimate": dv.__dict__,
                        **ctx.sample_info(n),
                    },
                )
            )
    return out


def _sampled_distinct(ctx: _Ctx, distinct: int, key: tuple[str, ...]) -> EstimatedValue:
    """Key-hash on exactly this key scales without bias; any other sample gives a lower bound."""
    s = ctx.sample
    assert s is not None
    if s.method == "keyhash" and s.key == key:
        scaled = scale_count(distinct, s.fraction)
        return EstimatedValue(scaled, None, None, "scaled", False)
    return EstimatedValue(distinct, distinct, None, "sample_lower_bound", True)


def _top_values(ctx: _Ctx, req: TopValues, p: Mapping[str, Any]) -> list[Fact]:
    distinct = _int(p.get("distinct"))
    non_null = _int(p.get("non_null"))
    if distinct is not None and distinct > ctx.config.enum_max_distinct:
        return []  # a truncated top-k of a high-cardinality column is not an enum
    ident = {"case_sensitive": True, "split": None}
    if not non_null:
        return [
            ctx.fact(
                FactKind.VALUES,
                req.column,
                {**ident, "observed": None, "reason": "no_values"},
                Status.UNMEASURABLE,
            )
        ]
    observed = [
        {"value": _plain(v.get("value")), "count": _int(v.get("count"))}
        for v in (p.get("values") or [])
    ]
    value: dict[str, Any] = {
        **ident,
        "observed": observed,
        "distinct": distinct,
        "non_null": non_null,
    }
    if ctx.sample is not None:
        # seen in the sample; a rare value absent from it is not claimed absent (spec 8.6)
        n = _int(p.get("sampled_rows", non_null))
        dv = EstimatedValue(distinct, distinct, None, "sample_lower_bound", True)
        value.update(
            seen_in_sample=True, complete=False, estimate=dv.__dict__, **ctx.sample_info(n)
        )
    return [ctx.fact(FactKind.VALUES, req.column, value)]


def _cardinality(ml: int | None, mr: int | None) -> str | None:
    if ml is None or mr is None:
        return None
    many_left, many_right = ml > 1, mr > 1
    if many_left and many_right:
        return "N:M"
    if many_left:
        return "N:1"
    if many_right:
        return "1:N"
    return "1:1"


def _join(ctx: _Ctx, req: KeyOverlapReq, p: Mapping[str, Any]) -> Fact:
    raw = {
        k: _int(p.get(k))
        for k in (
            "distinct_left",
            "matched",
            "distinct_right",
            "left_non_null",
            "matched_left_rows",
            "max_left_per_key",
            "max_right_per_key",
        )
    }
    value: dict[str, Any] = {k: None for k in JOIN_VALUE_KEYS}
    value["cast_required"] = req.join.cast_to is not None
    if not raw["distinct_left"]:
        value["reason"] = "no_left_keys"
        return ctx.fact(FactKind.JOIN, req.join, value, Status.UNMEASURABLE)
    dl, m, dr = raw["distinct_left"], raw["matched"], raw["distinct_right"]
    lnn, mlr = raw["left_non_null"], raw["matched_left_rows"]
    value.update(
        match_rate=_ratio(m, dl),
        coverage=_ratio(m, dr),
        row_match_rate=_ratio(mlr, lnn),
        fan_in=_ratio(lnn, dl),
        max_left_per_key=raw["max_left_per_key"],
        max_right_per_key=raw["max_right_per_key"],
        matched_left_rows=mlr,
        cardinality=_cardinality(raw["max_left_per_key"], raw["max_right_per_key"]),
    )
    g = ctx.config.graph
    value["verdict"] = join_verdict(value["match_rate"], g.ok_threshold, g.weak_threshold)
    s = ctx.sample
    if s is None:
        value.update(distinct_left=dl, distinct_right=dr, matched=m, left_non_null=lnn)
        return ctx.fact(FactKind.JOIN, req.join, value)

    def scaled(v: int | None) -> int | None:
        # key-hash keeps whole keys: key and row counts scale without bias
        return None if v is None or s.method != "keyhash" else scale_count(v, s.fraction)

    value.update(
        distinct_left=scaled(dl),
        distinct_right=scaled(dr),
        matched=scaled(m),
        left_non_null=scaled(lnn),
        matched_left_rows=scaled(mlr),
        sampled_left_rows=_int(p.get("sampled_left_rows")),
        sampled_right_rows=_int(p.get("sampled_right_rows")),
        fraction=ctx.sample_info(None)["fraction"],
    )
    estimates: dict[str, Any] = {}
    key_deff = 1.0 if s.method == "keyhash" else ctx.deff
    if m is not None and dl:
        estimates["match_rate"] = ctx.rate_estimate(m, dl, key_deff).__dict__
    if m is not None and dr and m <= dr:
        estimates["coverage"] = ctx.rate_estimate(m, dr, key_deff).__dict__
    if mlr is not None and lnn and mlr <= lnn:
        # rows cluster by key under key-hash sampling: widen by the design effect
        estimates["row_match_rate"] = ctx.rate_estimate(
            mlr, lnn, ctx.config.sampling.design_effect
        ).__dict__
    for k in ("max_left_per_key", "max_right_per_key"):
        if raw[k] is not None:
            estimates[k] = EstimatedValue(raw[k], raw[k], None, "sample_lower_bound", True).__dict__
    value["estimates"] = estimates
    value["estimate"] = estimates.get("match_rate")
    return ctx.fact(FactKind.JOIN, req.join, value)


def _uniqueness(ctx: _Ctx, req: Uniqueness, p: Mapping[str, Any]) -> Fact:
    rows = _int(p.get("row_count"))
    non_null = _int(p.get("non_null"))
    distinct = _int(p.get("distinct_keys"))
    dup = _int(p.get("duplicate_keys"))
    mx = _int(p.get("max_rows_per_key"))
    value: dict[str, Any] = {
        "columns": list(req.columns),
        "row_count": rows,
        "non_null": non_null,
        "distinct_keys": distinct,
        "duplicate_keys": dup,
        "max_rows_per_key": mx,
        "complete": None if rows is None or non_null is None else non_null == rows,
    }
    if not rows:
        value.update(unique=None, reason="empty_table" if rows == 0 else "missing_metric")
        return ctx.fact(FactKind.PRIMARY_KEY, req.table, value, Status.UNMEASURABLE)
    has_dup: bool | None = (dup > 0) if dup is not None else (mx > 1 if mx is not None else None)
    if ctx.sample is None:
        value["unique"] = None if has_dup is None else not has_dup
        return ctx.fact(FactKind.PRIMARY_KEY, req.table, value)
    # a key-hash sample keeps every row of a kept key: a duplicate seen is a real duplicate;
    # no duplicate seen confirms nothing (spec 8.6)
    value.update(
        unique=False if has_dup else None,
        consistent_with_sample=has_dup is False,
        **ctx.sample_info(_int(p.get("sampled_rows", rows))),
    )
    return ctx.fact(FactKind.PRIMARY_KEY, req.table, value)


def _as_datetime(v: Any) -> tuple[datetime | None, bool]:
    """``(aware datetime, assumed_utc)``; naive warehouse timestamps are read as UTC."""
    dt: datetime | None = None
    if isinstance(v, datetime):
        dt = v
    elif isinstance(v, date):
        dt = datetime(v.year, v.month, v.day)
    elif isinstance(v, str):
        text = v.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None, False
    if dt is None:
        return None, False
    if dt.tzinfo is None or dt.utcoffset() is None:
        return dt.replace(tzinfo=timezone.utc), True
    return dt, False


def _freshness(ctx: _Ctx, req: Freshness, p: Mapping[str, Any]) -> Fact:
    col = req.column.column
    subject = req.column.table_ref()
    mv = p.get("max_value")
    if mv is None:
        return ctx.fact(
            FactKind.FRESHNESS,
            subject,
            {
                "column": col,
                "max_value": None,
                "age_seconds": None,
                "reason": "no_values",
            },
            Status.UNMEASURABLE,
        )
    latest, assumed = _as_datetime(mv)
    now, _ = _as_datetime(p.get("server_now"))
    now = now or ctx.observed_at
    age = None if latest is None else (now - latest).total_seconds()
    value: dict[str, Any] = {"column": col, "max_value": _plain(mv), "age_seconds": age}
    if assumed:
        value["assumed_utc"] = True
    return ctx.fact(FactKind.FRESHNESS, subject, value)


def _pattern(ctx: _Ctx, req: PatternRate, p: Mapping[str, Any]) -> list[Fact]:
    ident = _type_note_identity(req)
    non_null = _int(p.get("non_null"))
    matching = _int(p.get("matching"))
    if not non_null or matching is None or matching > non_null:
        return [
            ctx.fact(
                FactKind.TYPE_NOTE,
                req.column,
                {**ident, "rate": None, "reason": "no_values"},
                Status.UNMEASURABLE,
            )
        ]
    value: dict[str, Any] = {
        **ident,
        "pattern": req.pattern,
        "matching": matching,
        "non_null": non_null,
    }
    if ctx.sample is None:
        rate = matching / non_null
        value.update(rate=rate, holds=rate >= PATTERN_HOLDS)
    else:
        est = ctx.rate_estimate(matching, non_null)
        value.update(
            rate=est.value,
            holds=None,
            estimate=est.__dict__,
            **ctx.sample_info(_int(p.get("sampled_rows", non_null))),
        )
    return [ctx.fact(FactKind.TYPE_NOTE, req.column, value)]
