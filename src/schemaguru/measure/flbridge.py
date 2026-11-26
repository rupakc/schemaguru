"""The FactLang seam for measurement (spec 5.2): SchemaGuru requests -> FactLang IR.

The only measurement module that imports ``factlang``. Every FactLang name is looked up at run
time, so an older or partial FactLang degrades to reported capabilities instead of import errors
(``capabilities()``; FactLang is built concurrently, see the global "FactLang availability"
rule). SchemaGuru never renders SQL here: it builds IR plans (``ColumnAggregate``,
``CompositeKeyAggregate``, ``ValueMembership(declared=())``, ``KeyOverlap`` with key tuples and
``right_sample``, ``PatternCoverage``) and hands them to ``Checker.measure`` /
``Checker.estimate`` / ``Checker.check``.
"""

from __future__ import annotations

import dataclasses
import importlib
from collections.abc import Mapping, Sequence
from typing import Any

from schemaguru.config import SamplingConfig
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
from schemaguru.measure.sampling import SEED_MAX
from schemaguru.model.evidence import Evidence
from schemaguru.model.refs import ObjectRef
from schemaguru.pii import ValuePolicy, scrub_tree, sql_template
from schemaguru.pii.policy import Mode

CAPABILITIES = (
    "measure",
    "estimate",
    "sample",
    "composite_join",
    "run_metadata",
    "render_join",
    "parse_filter",
)


def _module(name: str) -> Any | None:
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


def _attr(module: str, name: str) -> Any | None:
    mod = _module(module)
    return None if mod is None else getattr(mod, name, None)


def _fields(cls: Any) -> set[str]:
    if cls is None or not dataclasses.is_dataclass(cls):
        return set()
    return {f.name for f in dataclasses.fields(cls)}


def capabilities() -> frozenset[str]:
    """Which FactLang features are installed, detected by attribute presence."""
    caps: set[str] = set()
    checker = _attr("factlang", "Checker")
    if callable(getattr(checker, "measure", None)):
        caps.add("measure")
    if callable(getattr(checker, "estimate", None)):
        caps.add("estimate")
    key_overlap = _attr("factlang.ir", "KeyOverlap")
    if _attr("factlang.ir", "SampleSpec") is not None and "right_sample" in _fields(key_overlap):
        caps.add("sample")
    if {"left", "right"} <= _fields(key_overlap) and _attr(
        "factlang.ir", "CompositeKeyAggregate"
    ) is not None:
        caps.add("composite_join")
    if callable(_attr("factlang.connectors", "run_metadata")):
        caps.add("run_metadata")
    if callable(_attr("factlang.sql", "render_join")):
        caps.add("render_join")
    if callable(_attr("factlang", "parse_filter")):
        caps.add("parse_filter")
    return frozenset(caps)


def _num(v: Any) -> Any:
    """FactLang ``Decimal`` counts -> ``int``; other Decimals -> ``float``."""
    from decimal import Decimal

    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    return v


_SAFE_SEEDS = (0, SEED_MAX)  # inside every FactLang profile's sample_seed_range


class _IR:
    """Lazy handles on the FactLang IR names.

    ``seed_range`` is the dialect profile's ``sample_seed_range``. ``sampling.seed_for`` already
    yields seeds inside every profile's range; one outside is refused (the request errors),
    never folded, so the stored fact's method string names the seed FactLang ran.
    """

    def __init__(self, seed_range: tuple[int, int] = _SAFE_SEEDS) -> None:
        self.seed_range = seed_range
        ir = importlib.import_module("factlang.ir")  # public API (refs are re-exported here)
        self.ir = ir
        self.Metric = ir.Metric
        self.Ident = ir.Ident
        self.TableRef = ir.TableRef
        self.ColumnRef = ir.ColumnRef

    def ident(self, text: str) -> Any:
        # names are stored exactly as the catalog returns them: always quote (exact match)
        return self.Ident(text, True)

    def table(self, ref: ObjectRef) -> Any:
        return self.TableRef(
            None if ref.catalog is None else self.ident(ref.catalog),
            None if ref.schema is None else self.ident(ref.schema),
            self.ident(ref.table),
        )

    def column(self, ref: ObjectRef) -> Any:
        assert ref.column is not None
        return self.ColumnRef(self.table(ref.table_ref()), self.ident(ref.column))

    def sample(self, s: SampleChoice | None, key: Sequence[str] | None = None) -> Any:
        if s is None:
            return None
        cols = tuple(self.ident(k) for k in (key if key is not None else s.key))
        lo, hi = self.seed_range
        if not lo <= s.seed <= hi:  # never folded here: the fact records the seed that ran
            raise ValueError(f"sample seed {s.seed} is outside the dialect's range [{lo}, {hi}]")
        return self.ir.SampleSpec(
            s.method, s.fraction, s.seed, cols if s.method == "keyhash" else ()
        )

    def metrics(self, *names: str) -> tuple[Any, ...]:
        return tuple(getattr(self.Metric, n) for n in names)


def plans_for(req: Request, ir: _IR) -> list[Any]:
    """The FactLang IR plans for one request (several for a column profile; fused by FactLang)."""
    i = ir.ir
    s = ir.sample(req.sample)
    if isinstance(req, ProfileColumns):
        t = ir.table(req.table)
        if not req.columns:
            return [i.ColumnAggregate(t, None, ir.metrics("ROW_COUNT"), sample=s)]
        m = ir.metrics("ROW_COUNT", "NON_NULL", "DISTINCT_KEYS")
        return [i.ColumnAggregate(t, ir.ident(c), m, sample=s) for c in req.columns]
    if isinstance(req, TopValues):
        return [
            i.ValueMembership(
                ir.table(req.column.table_ref()),
                ir.ident(req.column.column or ""),
                (),
                report_limit=req.k,
                sample=s,
            )
        ]
    if isinstance(req, KeyOverlapReq):
        j = req.join
        right_s = (
            ir.sample(req.sample, [c.column or "" for c in j.right])
            if req.sample is not None and req.sample.method == "keyhash"
            else ir.sample(req.sample)
        )
        return [
            i.KeyOverlap(
                tuple(ir.column(c) for c in j.left),
                tuple(ir.column(c) for c in j.right),
                cast_to=j.cast_to,
                measure_multiplicity=req.multiplicity,
                sample=s,
                right_sample=right_s,
            )
        ]
    if isinstance(req, Uniqueness):
        m = ir.metrics("ROW_COUNT", "NON_NULL", "DISTINCT_KEYS", "MAX_PER_KEY", "DUPLICATE_KEYS")
        return [
            i.CompositeKeyAggregate(
                ir.table(req.table), tuple(ir.ident(c) for c in req.columns), m, sample=s
            )
        ]
    if isinstance(req, Freshness):
        return [
            i.ColumnAggregate(
                ir.table(req.column.table_ref()),
                ir.ident(req.column.column or ""),
                ir.metrics("MAX_VALUE", "NOW"),
            )
        ]
    if isinstance(req, PatternRate):
        return [
            i.PatternCoverage(
                ir.table(req.column.table_ref()),
                ir.ident(req.column.column or ""),
                req.pattern,
                sample=s,
            )
        ]
    assert isinstance(req, RowCount)
    return [i.ColumnAggregate(ir.table(req.table), None, ir.metrics("ROW_COUNT"))]


def _row(payload: Any) -> Mapping[str, Any]:
    return {k: _num(v) for k, v in dict(payload or {}).items()}


def payload_for(req: Request, payloads: Sequence[Any]) -> dict[str, Any] | None:
    """Assemble one request's SchemaGuru payload from its plans' FactLang payloads."""
    if all(p is None for p in payloads):
        return None
    if isinstance(req, ProfileColumns):
        rows = [_row(p) for p in payloads]
        out: dict[str, Any] = {"row_count": rows[0].get("row_count") if rows else None}
        if "sampled_rows" in rows[0]:
            out["sampled_rows"] = rows[0]["sampled_rows"]
        # no columns: one ROW_COUNT plan (``plans_for``), so no per-column metrics
        out["columns"] = {
            c: {"non_null": r.get("non_null"), "distinct_keys": r.get("distinct_keys")}
            for c, r in zip(req.columns, rows if req.columns else [], strict=True)
        }
        return out
    if isinstance(req, TopValues):
        rs = [_row(r) for r in (dict(payloads[0]).get("rows") or [])]
        summary = next((r for r in rs if r.get("kind") == "summary"), {})
        out = {
            "non_null": summary.get("non_null"),
            "distinct": summary.get("undeclared_distinct"),
            "values": [
                {"value": r.get("value"), "count": r.get("freq")}
                for r in rs
                if r.get("kind") == "undeclared"
            ],
        }
        if "sampled_rows" in summary:
            out["sampled_rows"] = summary["sampled_rows"]
        return out
    return dict(_row(payloads[0]))


_RESTRICTION = {"none": 0, "masked": 1, "raw": 2}


def _request_tables(req: Request) -> list[ObjectRef]:
    if isinstance(req, KeyOverlapReq):
        return [c.table_ref() for c in (*req.join.left, *req.join.right)]
    if isinstance(req, TopValues | Freshness | PatternRate):
        return [req.column.table_ref()]
    return [req.table]


def evidence_mode(req: Request | None, sampling: SamplingConfig) -> Mode:
    """How evidence for ``req`` may be stored (spec 11): the most restrictive table policy.

    ``raw`` survives only with ``store_raw_samples``; otherwise it is stored masked. Without a
    request (a free-form ``check``) every configured mode counts, so no glob is widened.
    """
    if req is None:
        modes = [sampling.llm_values, *sampling.per_glob.values()]
    else:
        modes = [ValuePolicy.for_table(t, sampling) for t in _request_tables(req)]
    mode = min(modes, key=lambda m: _RESTRICTION.get(m, 0))
    if mode == "raw" and not sampling.store_raw_samples:
        return "masked"
    return mode  # type: ignore[return-value]


def _plain_param(p: Any) -> Any:
    """A FactLang ``Param`` (``name``, ``value``, ``family``) as a plain dict; else unchanged.

    FactLang evidence carries ``Param`` dataclasses, which neither the scrubber (it walks only
    strings, mappings and sequences) nor JSON storage can see into.
    """
    if isinstance(p, Mapping) or not all(hasattr(p, a) for a in ("name", "value", "family")):
        return p
    return {"name": str(p.name), "family": str(p.family), "value": p.value}


def _scrub_params(params: Sequence[Any], mode: Mode) -> tuple[Any, ...]:
    """Parameters at any depth: ``none`` replaces every string with ``?``; else masked/raw.

    Of a FactLang ``Param`` only the value is data; its name and family are FactLang's own.
    """
    replacement = "?" if mode == "none" else None
    out: list[Any] = []
    for p in map(_plain_param, params):
        if isinstance(p, Mapping) and {"name", "family", "value"} <= set(p):
            value = scrub_tree(p["value"], mode, replacement=replacement)
            out.append({"name": p["name"], "family": p["family"], "value": value})
        else:
            out.append(scrub_tree(p, mode, replacement=replacement))
    return tuple(out)


def _evidence(
    fl_evidence: Any, plans_evidence: Sequence[Any], *, mode: Mode = "masked"
) -> Evidence | None:
    """FactLang evidence as stored: values scrubbed by ``mode``, SQL kept as a template only."""
    if fl_evidence is None:
        return None
    to_dict = getattr(fl_evidence, "to_dict", None)
    raw = to_dict() if callable(to_dict) else {}
    spent: list[Any] = [
        getattr(e, "bytes_processed", None) for e in plans_evidence if e is not None
    ]
    sample = getattr(fl_evidence, "sample", None)
    if sample is not None and not isinstance(sample, Mapping):
        sample = {
            "method": getattr(sample, "method", None),
            "fraction": str(getattr(sample, "fraction", "")),
            "seed": getattr(sample, "seed", None),
        }
    return _scrubbed(
        Evidence(
            claim_text=getattr(fl_evidence, "claim_text", None),
            verdict=getattr(fl_evidence, "verdict", None),
            sql=getattr(fl_evidence, "sql", None),
            params=tuple(getattr(fl_evidence, "params", ()) or ()),
            dialect=getattr(fl_evidence, "dialect", None),
            tier=getattr(fl_evidence, "tier", None),
            duration_ms=getattr(fl_evidence, "duration_ms", None),
            bytes_processed=None if any(b is None for b in spent) else sum(int(b) for b in spent),
            sample=sample,
            raw=dict(raw) if isinstance(raw, Mapping) else {},
        ),
        mode,
    )


def _charged_bytes(
    plans: Sequence[Any], ms: Sequence[Any], spans: Sequence[tuple[int, int]]
) -> dict[tuple[int, int], int | None]:
    """The bytes each request (its span of ``plans``) is charged, each query counted once.

    FactLang fuses plans into one query and gives every member's evidence that query's whole
    ``bytes_processed`` plus the keys it was ``fused_with``; identical plans share one query
    too. A query's bytes go to the first request it served (0 to the later ones); a query
    whose bytes are unknown leaves every request it served unknown (charged by estimate)."""
    canonical_key = _attr("factlang.ir", "canonical_key")

    def key(i: int) -> str:
        try:
            return str(canonical_key(plans[i])) if callable(canonical_key) else f"#unkeyed-{i}"
        except Exception:
            return f"#unkeyed-{i}"

    seen: set[frozenset[str]] = set()
    out: dict[tuple[int, int], int | None] = {}
    for a, b in spans:
        total: int | None = 0
        for i in range(a, b):
            ev = getattr(ms[i], "evidence", None)
            if ev is None:
                continue
            spent = getattr(ev, "bytes_processed", None)
            query = frozenset({key(i), *(str(k) for k in getattr(ev, "fused_with", ()) or ())})
            if spent is None:
                total = None
            elif query not in seen and total is not None:
                total += int(spent)
            seen.add(query)
        out[(a, b)] = total
    return out


def _scrubbed(ev: Evidence, mode: Mode) -> Evidence:
    return dataclasses.replace(
        ev,
        claim_text=None if ev.claim_text is None else scrub_tree(ev.claim_text, mode),
        sql=None if ev.sql is None else sql_template(ev.sql),
        params=_scrub_params(ev.params, mode),
        raw=scrub_tree(dict(ev.raw), mode),
        sample=None if ev.sample is None else scrub_tree(dict(ev.sample), mode),
    )


def _diagnostic(err: Any) -> Diagnostic:
    code = str(getattr(err, "code", "") or "")
    message = str(getattr(err, "message", err))
    if code and message.startswith(code):  # FactLang messages may already lead with the code
        message = message[len(code) :].lstrip()
    if code == "FL701":  # FactLang's own cost guard: over budget
        return Diagnostic("SG301", Severity.ERROR, f"{code} {message}".strip())
    return Diagnostic("SG303", Severity.ERROR, f"{code} {message}".strip())


NO_BLOCK_SAMPLING = "no_block_sampling"  # == measure.scheduler.NO_BLOCK_SAMPLING


def _profile(dialect: Any) -> Any | None:
    """FactLang's native profile for ``dialect``, or None (bridged/unknown)."""
    get_profile = _attr("factlang.sql", "get_profile")
    if not callable(get_profile) or not isinstance(dialect, str):
        return None
    try:
        return get_profile(dialect)
    except Exception:
        return None


def _dialect_caps(profile: Any | None) -> frozenset[str]:
    """Per-connector capabilities: ``no_block_sampling`` when FactLang's profile for the
    connector's dialect cannot render a ``system`` sample (it raises FL506; e.g. DuckDB, whose
    block sample is not repeatable), so the scheduler asks for ``bernoulli`` instead."""
    styles = getattr(profile, "sample_styles", None)
    if isinstance(styles, Mapping) and styles.get("system") is None:
        return frozenset({NO_BLOCK_SAMPLING})
    return frozenset()


def _seed_range(profile: Any | None) -> tuple[int, int]:
    """The profile's ``sample_seed_range`` intersected with ``[0, 2**31 - 1]``.

    The profile's range is not enough on its own: DuckDB's is ``[0, 2**63 - 1]``, yet DuckDB 1.5
    (the last release for Python 3.10) fails to parse a ``REPEATABLE`` seed above 32 bits.
    """
    rng = getattr(profile, "sample_seed_range", None)
    if isinstance(rng, tuple) and len(rng) == 2 and all(isinstance(v, int) for v in rng):
        lo, hi = max(rng[0], _SAFE_SEEDS[0]), min(rng[1], _SAFE_SEEDS[1])
        if lo <= hi:
            return lo, hi
    return _SAFE_SEEDS


class FactLangBackend:
    """``MeasureBackend`` over a FactLang connector and a store-backed catalog."""

    def __init__(
        self,
        connector: Any,
        *,
        catalog: Any,
        sampling: SamplingConfig | None = None,
        dialect: str | None = None,
        **checker_kwargs: Any,
    ) -> None:
        self._sampling = sampling if sampling is not None else SamplingConfig()
        self._catalog = catalog
        dialect = dialect or getattr(connector, "dialect", None)
        profile = _profile(dialect)
        self._caps = capabilities() | _dialect_caps(profile)
        self._checker: Any = None
        self._ir: _IR | None = None
        self._unavailable = "Checker.measure"
        if "measure" in self._caps:
            checker_cls = _attr("factlang", "Checker")
            assert checker_cls is not None
            if dialect is not None:  # FactLang's Checker takes the dialect from the connector
                checker_kwargs.setdefault("dialect", dialect)
            try:
                self._checker = checker_cls(connector, catalog=catalog, **checker_kwargs)
            except Exception as e:  # a connector FactLang cannot use: reported per request
                self._unavailable = f"a Checker for this connector ({type(e).__name__}: {e})"
            else:
                self._ir = _IR(_seed_range(profile))

    def capabilities(self) -> frozenset[str]:
        return self._caps

    def _missing(self, reqs: Sequence[Request], what: str) -> list[MeasureResult]:
        d = Diagnostic("SG304", Severity.ERROR, f"the installed FactLang does not provide {what}")
        return [MeasureResult(r, None, None, d) for r in reqs]

    def measure(self, reqs: Sequence[Request]) -> list[MeasureResult]:
        if self._checker is None or self._ir is None:
            return self._missing(reqs, self._unavailable)
        if any(r.sample is not None for r in reqs) and "sample" not in self._caps:
            return self._missing(reqs, "sampled measurement (SampleSpec)")
        spans: list[tuple[int, int]] = []
        plans: list[Any] = []
        out: list[MeasureResult | None] = [None] * len(reqs)
        for n, r in enumerate(reqs):
            try:
                ps = plans_for(r, self._ir)
            except (TypeError, ValueError) as e:  # FactLang refused the plan: this request only
                out[n] = MeasureResult(r, None, None, Diagnostic("SG303", Severity.ERROR, str(e)))
                spans.append((len(plans), len(plans)))
                continue
            spans.append((len(plans), len(plans) + len(ps)))
            plans += ps
        try:
            ms = list(self._checker.measure(plans)) if plans else []
            if len(ms) != len(plans):
                raise RuntimeError(f"{len(ms)} measurements for {len(plans)} plans")
        except Exception as e:  # FactLang promises not to raise; isolate it if it does
            d = Diagnostic("SG303", Severity.ERROR, f"FactLang measure failed: {e}")
            return [o or MeasureResult(r, None, None, d) for o, r in zip(out, reqs, strict=True)]
        charged = _charged_bytes(plans, ms, [s for n, s in enumerate(spans) if out[n] is None])
        for n, (r, (a, b)) in enumerate(zip(reqs, spans, strict=True)):
            if out[n] is not None:
                continue
            mine = ms[a:b]
            err = next((m.error for m in mine if getattr(m, "error", None) is not None), None)
            evs = [getattr(m, "evidence", None) for m in mine]
            ev = _evidence(evs[0] if evs else None, evs, mode=evidence_mode(r, self._sampling))
            if ev is not None:
                ev = dataclasses.replace(ev, bytes_processed=charged.get((a, b)))
            if err is not None:
                out[n] = MeasureResult(r, None, ev, _diagnostic(err))
            else:
                payload = payload_for(r, [m.payload for m in mine])
                if isinstance(r, TopValues) and payload is not None:
                    payload = self._typed_values(r.column, payload)
                out[n] = MeasureResult(r, payload, ev, None)
        return [o for o in out if o is not None]

    def _typed_values(self, column: ObjectRef, payload: dict[str, Any]) -> dict[str, Any]:
        """FactLang reports observed values as text (``'true'``); a boolean column's values are
        booleans again, so they render ``TRUE``/``FALSE`` and feed the soft-delete marker."""
        family = getattr(getattr(self, "_catalog", None), "family", None)
        try:
            boolean = callable(family) and family(column) == "boolean"
        except Exception:
            boolean = False
        if not boolean:
            return payload
        as_bool = {"true": True, "false": False, "t": True, "f": False, "1": True, "0": False}
        values = []
        for v in payload.get("values") or []:
            raw = v.get("value")
            key = raw.strip().lower() if isinstance(raw, str) else None
            values.append({**v, "value": as_bool[key]} if key in as_bool else v)
        return {**payload, "values": values}

    def estimate(self, reqs: Sequence[Request]) -> list[CostEstimate]:
        if self._checker is None or self._ir is None or "estimate" not in self._caps:
            return [CostEstimate(r, None, "unknown") for r in reqs]
        out: list[CostEstimate] = []
        for r in reqs:
            try:
                ests = list(self._checker.estimate(plans_for(r, self._ir)))
            except Exception:
                out.append(CostEstimate(r, None, "unknown"))
                continue
            sizes: list[Any] = [getattr(e, "bytes", None) for e in ests]
            if not ests or any(b is None for b in sizes):
                out.append(CostEstimate(r, None, "unknown"))
            else:
                out.append(CostEstimate(r, sum(int(b) for b in sizes), "dry_run"))
        return out

    def check(self, claims: str) -> list[Evidence]:
        if self._checker is None:
            return []
        report = self._checker.check(claims)
        out: list[Evidence] = []
        for o in getattr(report, "outcomes", ()):
            ev = getattr(o, "evidence", None)
            raw = o.to_dict() if callable(getattr(o, "to_dict", None)) else {}
            verdict = getattr(o, "verdict", None)
            out.append(
                _scrubbed(
                    Evidence(
                        claim_text=getattr(o, "claim_text", None) or getattr(o, "text", None),
                        verdict=getattr(verdict, "value", verdict),
                        sql=getattr(ev, "sql", None),
                        params=tuple(getattr(ev, "params", ()) or ()),
                        dialect=getattr(ev, "dialect", None),
                        tier=getattr(ev, "tier", None),
                        duration_ms=getattr(ev, "duration_ms", None),
                        bytes_processed=getattr(ev, "bytes_processed", None),
                        raw=dict(raw) if isinstance(raw, Mapping) else {},
                    ),
                    evidence_mode(None, self._sampling),
                )
            )
        return out


def scrub_evidence(ev: Evidence, mode: Mode) -> Evidence:
    """``ev`` as it may leave SchemaGuru under ``mode`` (SQL templated, values scrubbed)."""
    return _scrubbed(ev, mode)


def count_claims(text: str) -> int:
    """Claims in ``text`` by FactLang's own line rule (``factlang.agent.tools.count_claims``).

    When the installed FactLang lacks it, the count is the number of non-blank lines: a count
    that can only be too high, so a cap built on it refuses too much, never too little.
    """
    counter = _attr("factlang.agent.tools", "count_claims")
    if callable(counter):
        try:
            return int(counter(text))
        except Exception:
            pass
    return sum(1 for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n") if ln.strip())


def check_tool_definition() -> dict[str, Any] | None:
    """FactLang's ``factlang_check`` agent tool spec, or None when the installed FactLang has
    no agent surface (``factlang.agent.tool_definitions`` + ``handle_tool_call``).

    Only the definition crosses the seam (description, ``input_schema``); claims still run
    through ``FactLangBackend.check``, so their evidence is scrubbed by the value policy.
    """
    defs = _attr("factlang.agent", "tool_definitions")
    if not callable(defs) or not callable(_attr("factlang.agent", "handle_tool_call")):
        return None
    try:
        specs = list(defs())
    except Exception:
        return None
    for spec in specs:
        if isinstance(spec, Mapping) and spec.get("name") == "factlang_check":
            schema = spec.get("input_schema")
            if isinstance(schema, Mapping) and isinstance(spec.get("description"), str):
                return {
                    "name": "factlang_check",
                    "description": str(spec["description"]),
                    "input_schema": dict(schema),
                }
    return None
