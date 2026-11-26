"""Measurement results -> facts (spec 4.3-4.5, 8.1, 8.5, 8.6).

* exact -> ``OBSERVED`` with ``method="exact"``; sampled -> ``ESTIMATED`` with the
  ``sampled:<method>:<fraction>:<seed>`` method and ``value["estimate"]`` (an
  :class:`EstimatedValue` as a dict). Sampled numbers are never ``VERIFIED``.
* a probe error -> one ``ERROR`` fact per subject the probe would have produced.
* an empty table -> ``UNMEASURABLE``, distinct from an all-null column (``NULL_RATE`` 1.0,
  ``dead``).
* unknown is ``None``, never 0 or 1.0; counts are ``int``, rates ``float``.
* observed values pass through the table's value policy before they are stored (spec 11):
  ``masked`` (and ``raw`` unless ``store_raw_samples``) keeps shape tokens and records a ``PII``
  fact; ``none`` keeps counts only.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from schemaguru._util.redact import scrub_text
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
from schemaguru.model.facts import Fact, Provenance, Status, method_class
from schemaguru.model.joins import JOIN_VALUE_KEYS, JoinRef, join_verdict
from schemaguru.model.kinds import FactKind, KindSpec
from schemaguru.model.refs import ObjectRef
from schemaguru.pii import ValuePolicy, mask_free_text, mask_value

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
        self.approximate = self.sample is not None and self.sample.method == "system"

    def fact(
        self,
        kind: KindSpec,
        subject: ObjectRef | JoinRef,
        value: Mapping[str, Any],
        status: Status | None = None,
    ) -> Fact:
        if status is None:
            status = Status.OBSERVED if self.sample is None else Status.ESTIMATED
        payload = dict(value)
        if status is Status.ESTIMATED and self.approximate:
            payload["approximate"] = True  # block sample: clustered, interval widened
        return Fact.make(
            kind,
            subject,
            payload,
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

    def ev(
        self,
        value: float | int | None,
        low: float | None,
        high: float | None,
        method: str,
        lower_bound: bool,
    ) -> EstimatedValue:
        return EstimatedValue(value, low, high, method, lower_bound, self.approximate)

    def rate_estimate(self, successes: int, n: int, deff: float | None = None) -> EstimatedValue:
        ci = wilson(successes, n, design_effect=self.deff if deff is None else deff)
        lo, hi = (None, None) if ci is None else ci
        return self.ev(_ratio(successes, n), lo, hi, WILSON, False)


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
                {
                    **ident,
                    "reason": reason,
                    "code": code,
                    # warehouse errors can quote the offending value back, and a driver's
                    # message can echo the connection's credentials
                    "message": mask_free_text(scrub_text(result.error.message))[0],
                },
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
        return _uniqueness(ctx, req, p)
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
        return ctx.ev(scaled, None, None, "scaled", False)
    return ctx.ev(distinct, distinct, None, "sample_lower_bound", True)


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
    s = ctx.sample
    raw_non_null = non_null
    scaled: dict[str, Any] = {}
    if s is not None and s.method == "keyhash":
        # a key-hash row sample keeps whole rows at `fraction`: counts scale without bias
        scaled["sample_counts"] = {
            "non_null": non_null,
            "observed": [o["count"] for o in observed],
        }
        observed = [
            {**o, "count": None if o["count"] is None else scale_count(o["count"], s.fraction)}
            for o in observed
        ]
        non_null = scale_count(non_null, s.fraction)
    mode, stored, categories = _apply_policy(ctx, req.column, observed)
    value: dict[str, Any] = {**ident}
    if stored is not None:
        value["observed"] = stored
    value.update(
        distinct=distinct, non_null=non_null, distinct_observed=len(observed), value_policy=mode
    )
    if categories:
        value["masked_categories"] = list(categories)
    if stored is not None and len(stored) < len(observed):
        value["merged"] = True  # masking collapsed distinct values into one token
    if ctx.sample is not None:
        # seen in the sample; a rare value absent from it is not claimed absent (spec 8.6)
        n = _int(p.get("sampled_rows", raw_non_null))
        dv = ctx.ev(distinct, distinct, None, "sample_lower_bound", True)
        value.update(
            seen_in_sample=True, complete=False, estimate=dv.__dict__, **ctx.sample_info(n)
        )
        if scaled:
            value.update(scaled)
            value["estimates"] = {
                "non_null": ctx.ev(non_null, None, None, "scaled", False).__dict__,
                "distinct": dv.__dict__,
            }
    # the classification is as good as the values it was read from: a sampled probe's PII
    # fact carries the sample's method and is an estimate (spec 8.6)
    pii = [
        ctx.fact(FactKind.PII, req.column, {"category": cat, "detector": cat}) for cat in categories
    ]
    return [ctx.fact(FactKind.VALUES, req.column, value), *pii]


def _apply_policy(
    ctx: _Ctx, column: ObjectRef, observed: list[dict[str, Any]]
) -> tuple[str, list[dict[str, Any]] | None, list[str]]:
    """``(stored mode, values to store or None, PII categories seen)`` (spec 11).

    The stored mode is ``raw`` only when raw values were actually kept.

    ``none`` stores no values; ``raw`` stores them verbatim only with ``store_raw_samples``;
    otherwise each value is masked and identical tokens are merged by summing their counts.
    """
    mode = ValuePolicy.for_table(column, ctx.config.sampling)
    if mode == "none":
        return "none", None, []
    if mode == "raw" and ctx.config.sampling.store_raw_samples:
        return "raw", observed, []
    merged: dict[Any, dict[str, Any]] = {}
    categories: list[str] = []
    for item in observed:
        v, n = item["value"], item["count"]
        if v is not None:
            masked, cat = mask_value(v)
            if cat is not None:
                v = masked
                if cat not in categories:
                    categories.append(cat)
        key = (type(v).__name__, v)
        prev = merged.get(key)
        if prev is None:
            merged[key] = {"value": v, "count": n}
        elif n is not None:
            prev["count"] = n if prev["count"] is None else prev["count"] + n
    return "masked", list(merged.values()), categories


def _distinct_rank(f: Fact) -> int:
    """Lower wins: exact count, keyhash (scaled, unbiased) estimate, other sample, unusable."""
    if f.status is Status.OBSERVED and method_class(f.method) == "exact":
        return 0
    if f.status is Status.ESTIMATED:
        est = f.value.get("estimate")
        return 1 if isinstance(est, Mapping) and est.get("method") == "scaled" else 2
    return 3


def _distinct_order(f: Fact) -> tuple[int, str, str, str]:
    value = json.dumps(dict(f.value), sort_keys=True, default=str)
    return (_distinct_rank(f), f.method or "", value, f.id)


def resolve_distinct(facts: Sequence[Fact]) -> list[Fact]:
    """One ``DISTINCT`` fact per column from one batch, whatever order the batch came in.

    A column profile and a key-hash uniqueness probe can both measure a column's distinct count.
    The exact count wins over any estimate, a key-hash estimate (whole keys kept: scaled without
    bias) over a row sample's, and any number over an unmeasurable one. Equal ranks (facts of
    one column share an id) go to the lower method string, then value. Error facts and every
    other kind are kept as they are; the winner takes the place of the column's first
    ``DISTINCT`` fact."""
    best: dict[str, Fact] = {}
    for f in facts:
        if f.kind.name != "DISTINCT" or f.status is Status.ERROR:
            continue
        key = f.subject.canonical()
        cur = best.get(key)
        if cur is None or _distinct_order(f) < _distinct_order(cur):
            best[key] = f
    out: list[Fact] = []
    placed: set[str] = set()
    for f in facts:
        if f.kind.name != "DISTINCT" or f.status is Status.ERROR:
            out.append(f)
            continue
        key = f.subject.canonical()
        if key not in placed:
            placed.add(key)
            out.append(best[key])
    return out


def _cardinality(ml: int | None, mr: int | None, *, sampled: bool = False) -> str | None:
    """``1:1``/``N:1``/``1:N``/``N:M`` from the largest rows per key on each side. Sampled, a
    repeat seen on a side is real (it refutes "1" there) but a max of 1 proves nothing (keys
    outside the sample may repeat): that side is ``?`` (``N:?``, ``?:N``), and a sample that saw
    no repeat on either side gives ``None``, never ``1:1``."""
    if ml is None or mr is None:
        return None
    if sampled:
        if ml > 1 and mr > 1:
            return "N:M"
        if ml > 1:
            return "N:?"
        return "?:N" if mr > 1 else None
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
        cardinality=_cardinality(
            raw["max_left_per_key"], raw["max_right_per_key"], sampled=ctx.sample is not None
        ),
    )
    if (
        ctx.sample is not None
        and value["cardinality"] is None
        and None not in (raw["max_left_per_key"], raw["max_right_per_key"])
    ):
        value["cardinality_reason"] = "sampled"  # a sampled max of 1 is only a lower bound
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
            estimates[k] = ctx.ev(raw[k], raw[k], None, "sample_lower_bound", True).__dict__
    value["estimates"] = estimates
    value["estimate"] = estimates.get("match_rate")
    return ctx.fact(FactKind.JOIN, req.join, value)


def _uniqueness(ctx: _Ctx, req: Uniqueness, p: Mapping[str, Any]) -> list[Fact]:
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
        return [ctx.fact(FactKind.PRIMARY_KEY, req.table, value, Status.UNMEASURABLE)]
    has_dup: bool | None = (dup > 0) if dup is not None else (mx > 1 if mx is not None else None)
    s = ctx.sample
    if s is None:
        value["unique"] = None if has_dup is None else not has_dup
        return [ctx.fact(FactKind.PRIMARY_KEY, req.table, value)]
    # a key-hash sample keeps every row of a kept key: a duplicate seen is a real duplicate;
    # no duplicate seen confirms nothing (spec 8.6). A row missing a key part in the sample is a
    # real incomplete row; none seen confirms nothing either.
    value.update(
        unique=False if has_dup else None,
        consistent_with_sample=has_dup is False,
        complete=False if value["complete"] is False else None,
        **ctx.sample_info(_int(p.get("sampled_rows", rows))),
    )
    estimates: dict[str, Any] = {}
    out: list[Fact] = []
    if s.method == "keyhash" and s.key == req.columns:
        # whole keys kept at `fraction`: key and row counts scale without bias
        for k, raw in (
            ("row_count", rows),
            ("non_null", non_null),
            ("distinct_keys", distinct),
            ("duplicate_keys", dup),
        ):
            scaled = None if raw is None else scale_count(raw, s.fraction)
            value[k] = scaled
            if scaled is not None:
                estimates[k] = ctx.ev(scaled, None, None, "scaled", False).__dict__
        if len(req.columns) == 1 and "distinct_keys" in estimates:
            t = req.table
            col = ObjectRef(t.catalog, t.schema, t.table, req.columns[0])
            out.append(
                ctx.fact(
                    FactKind.DISTINCT,
                    col,
                    {
                        "distinct_keys": value["distinct_keys"],
                        "estimate": estimates["distinct_keys"],
                        **ctx.sample_info(_int(p.get("sampled_rows", rows))),
                    },
                )
            )
    else:
        # not key-hash on this key: the raw sample counts are no estimate of the totals
        value["sample_counts"] = {
            "row_count": rows,
            "non_null": non_null,
            "distinct_keys": distinct,
            "duplicate_keys": dup,
        }
        value.update(row_count=None, non_null=None, distinct_keys=None, duplicate_keys=None)
        if distinct is not None:
            estimates["distinct_keys"] = ctx.ev(
                distinct, distinct, None, "sample_lower_bound", True
            ).__dict__
    if mx is not None:
        estimates["max_rows_per_key"] = ctx.ev(mx, mx, None, "sample_lower_bound", True).__dict__
    value["estimates"] = estimates
    value["estimate"] = estimates.get("distinct_keys")
    return [ctx.fact(FactKind.PRIMARY_KEY, req.table, value), *out]


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
