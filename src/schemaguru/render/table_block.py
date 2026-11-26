"""Table blocks (spec 13.3): analysis of one table's knowledge, and the agent rendering.

Rules, all enforced here once and shared by every built-in profile:

* consequence order: ``⚠`` lines come right after the header, before anything that confirms;
* hedges (unmeasured, sampled) sit above the lines they qualify;
* numbers carry 4 significant digits; exact integers are printed in full;
* an observed number says ``measured <date>``, a passed claim ``verified <date>``, a sampled
  number ``≈ v (sampled f%, ±h)`` (or ``≥ v (..., lower bound)``) and never "verified";
* a drafted fact that is not VERIFIED is tagged ``(draft)`` whatever its status;
* value lists are capped at ``max_values_shown`` with ``+N more``;
* thin joins state their coverage as a number.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from typing import Any

from schemaguru.graph.search import Path
from schemaguru.knowledge.columns import LITE_MAX_COLUMNS, lite_columns
from schemaguru.knowledge.resolve import Effective
from schemaguru.knowledge.soft_delete import MISMATCH
from schemaguru.model.detail import DetailLevel
from schemaguru.model.facts import Fact, Provenance, Status, method_class
from schemaguru.model.joins import JoinRef, join_verdict
from schemaguru.model.lineage import source_label
from schemaguru.model.refs import ObjectRef
from schemaguru.model.structure import Column
from schemaguru.pii.mask import mask_free_text, mask_value
from schemaguru.pii.policy import ValuePolicy
from schemaguru.pii.sweep import sweep
from schemaguru.render.knowledge import TableKnowledge
from schemaguru.render.legend import WARN

# A note is shown at every level whose rank is <= the note's rank.
RANK = {DetailLevel.FULL: 0, DetailLevel.STANDARD: 1, DetailLevel.LITE: 2, DetailLevel.NAME_ONLY: 3}
FULL_ONLY, STANDARD_UP, LITE_UP = 0, 1, 2
INBOUND_SHOWN = 3
_P, _S = Provenance, Status
_UNUSABLE = (_S.ERROR, _S.UNMEASURABLE)
_CASE_DRIFT = " differ only in case — compare case-insensitively"
_HIDDEN = "(values hidden by policy)"
# A whole masking token (``<email>``, ``<hex32>``, ``<free_text len=40>``, ``<masked>``).
TOKEN = re.compile(r"<[a-z_]+(?:\d+| len=\d+)?>\Z")
_LITERAL = re.compile(r"'(?:[^']|'')*'")

# ----- numbers and words ---------------------------------------------------------------


def sig4(x: float | int, *, floor: bool = False) -> str:
    """``x`` to 4 significant digits, no exponent, thousands separated (``1,235,000``).

    ``floor`` rounds toward minus infinity (a lower bound must never be rounded up); an integer
    lower bound is printed in full.
    """
    if isinstance(x, bool) or not math.isfinite(float(x)):
        return "unknown"
    if isinstance(x, int) and (abs(x) < 10_000 or floor):
        return f"{x:,}"
    if floor and float(x) != 0:
        d = Decimal(repr(float(x)))
        exp = d.adjusted() - 3  # keep 4 significant digits
        text = format(d.quantize(Decimal(1).scaleb(exp), rounding=ROUND_FLOOR), "f")
    else:
        text = format(Decimal(f"{float(x):.4g}"), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    sign = "-" if text.startswith("-") else ""
    whole, _, frac = text.lstrip("-").partition(".")
    whole = f"{int(whole):,}"
    out = sign + whole + (f".{frac}" if frac else "")
    return "0" if out in ("-0", "") else out


def plain4(x: float | int) -> str:
    """:func:`sig4` without thousands separators (machine-parseable lines)."""
    return sig4(x).replace(",", "")


def pct(rate: float | Decimal, *, floor: bool = False) -> str:
    """A rate in [0, 1] as a percentage to 4 significant digits (``0.998`` -> ``99.8%``).

    Rounding never claims certainty: a rate that is not exactly 1 never prints ``100%``
    (``>99.99%``), and one that is not exactly 0 never prints ``0%`` (``<0.01%``).
    """
    r = float(rate)
    if 0 < r < 0.0001:
        return "<0.01%"
    if 0.9999 < r < 1:
        return ">99.99%"
    text = sig4(r * 100, floor=floor)
    if text == "100" and r < 1:
        return ">99.99%"
    if text == "0" and r > 0:
        return "<0.01%"
    return text + "%"


def count(n: int) -> str:
    return f"{n:,}"


def rows(n: int) -> str:
    return f"{count(n)} row" + ("" if n == 1 else "s")


def day(dt: datetime | None) -> str | None:
    return None if dt is None else dt.date().isoformat()


# Words reserved in most SQL dialects: a bare one would not parse as a name.
RESERVED = frozenset(
    [
        "all",
        "alter",
        "and",
        "any",
        "as",
        "asc",
        "between",
        "both",
        "by",
        "case",
        "cast",
        "check",
        "column",
        "constraint",
        "create",
        "cross",
        "current_date",
        "current_time",
        "current_timestamp",
        "current_user",
        "date",
        "default",
        "delete",
        "desc",
        "distinct",
        "drop",
        "else",
        "end",
        "except",
        "exists",
        "false",
        "fetch",
        "for",
        "foreign",
        "from",
        "full",
        "grant",
        "group",
        "having",
        "in",
        "index",
        "inner",
        "insert",
        "intersect",
        "interval",
        "into",
        "is",
        "join",
        "key",
        "lateral",
        "leading",
        "left",
        "like",
        "limit",
        "natural",
        "not",
        "null",
        "offset",
        "on",
        "or",
        "order",
        "outer",
        "over",
        "partition",
        "primary",
        "qualify",
        "range",
        "references",
        "right",
        "rows",
        "select",
        "session_user",
        "some",
        "table",
        "then",
        "time",
        "timestamp",
        "to",
        "trailing",
        "true",
        "union",
        "unique",
        "update",
        "user",
        "using",
        "values",
        "when",
        "where",
        "window",
        "with",
    ]
)
_BARE = re.compile(r"[a-z_][a-z0-9_]*\Z")


def ident(name: str) -> str:
    """A name as SQL text: bare only when lower-case, plain and not a reserved word.

    Mixed-case names (``Orders``) are quoted because case-folding dialects would otherwise read
    them as ``orders``; reserved words (``select``) and odd names (``Line No``) need quotes too.
    """
    if _BARE.match(name) and name not in RESERVED:
        return name
    return '"' + name.replace('"', '""') + '"'


def qname(ref: ObjectRef) -> str:
    """A (possibly qualified) reference, each part quoted by :func:`ident`."""
    parts = (ref.catalog, ref.schema, ref.table, ref.column)
    return ".".join(ident(p) for p in parts if p is not None)


def quote(v: object) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def capped(items: Sequence[str], cap: int, sep: str = ",") -> str:
    shown = sep.join(items[: max(cap, 0)])
    more = len(items) - max(cap, 0)
    return shown + (f" +{more} more" if more > 0 else "")


def _fraction(raw: object) -> Decimal | None:
    if raw is None:
        return None
    try:
        d = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None
    return d if Decimal(0) < d <= 1 else None


def fraction_of(f: Fact) -> str | None:
    """The sample fraction of a sampled fact, from its value or its ``sampled:...`` method."""
    raw = f.value.get("fraction")
    if _fraction(raw) is None and (f.method or "").startswith("sampled:"):
        parts = (f.method or "").split(":")
        raw = parts[2] if len(parts) > 2 else None
    return None if _fraction(raw) is None else str(raw)


def estimate_text(est: Mapping[str, Any], fraction: str | None, *, rate: bool = True) -> str:
    """``≈ 3.2% (sampled 1%, ±0.4%)``; ``≥ v (sampled f%, lower bound)``; spec 4.4, 8.6."""
    v = est.get("value")
    if v is None or isinstance(v, bool):
        return "unknown"

    def num(x: float | int, floor: bool = False) -> str:
        return pct(x, floor=floor) if rate else sig4(x, floor=floor)

    frac = _fraction(fraction)
    parts = [f"sampled {pct(frac)}" if frac is not None else "sampled"]
    if est.get("lower_bound"):
        return f"≥ {num(v, True)} ({parts[0]}, lower bound)"
    low, high = est.get("low"), est.get("high")
    if isinstance(low, (int, float)) and isinstance(high, (int, float)):
        lo, hi, mid = float(low), float(high), float(v)
        if (rate and (lo <= 0 or hi >= 1)) or abs((hi - mid) - (mid - lo)) > 0.1 * (hi - lo):
            parts.append(f"{num(lo, True)} to {num(hi)}")  # asymmetric (near 0 or 1): the range
        else:
            parts.append("±" + num((hi - lo) / 2))
    if est.get("approximate"):
        parts.append("approximate")
    return f"≈ {num(v)} ({', '.join(parts)})"


def join_number(m: Fact, key: str) -> str:
    """A join rate of measurement ``m``; a sampled one is always ``≈`` (spec 8.6)."""
    val = m.value.get(key)
    sampled = m.status is _S.ESTIMATED or method_class(m.method) == "sampled"
    ests = m.value.get("estimates")
    est = ests.get(key) if isinstance(ests, Mapping) else None
    frac = fraction_of(m)
    if sampled and isinstance(est, Mapping):
        return estimate_text(est, frac)
    if not isinstance(val, (int, float)) or isinstance(val, bool):
        return "unknown"
    if sampled:  # an estimate even when no interval was stored
        f = _fraction(frac)
        return f"≈ {pct(val)} (sampled{' ' + pct(f) if f is not None else ''})"
    return pct(val)


# ----- fact phrases ------------------------------------------------------------------------


def is_estimate(f: Fact) -> bool:
    """A sampled number: ESTIMATED, or any later status (STALE, CONTRADICTED) of a sample."""
    return f.status is _S.ESTIMATED or method_class(f.method) == "sampled"


def is_draft(f: Fact) -> bool:
    """Drafted and not VERIFIED: tagged ``(draft)`` whatever the status (ERROR included)."""
    return f.provenance is _P.DRAFTED and f.status is not _S.VERIFIED


def tag(f: Fact) -> str:
    return " (draft)" if is_draft(f) else ""


def phrase(f: Fact) -> str:
    """How a fact is known, in words: never "verified" unless a claim passed."""
    d = day(f.observed_at)
    s, p = f.status, f.provenance
    # a measured or catalog fact of unknown age is never stale (base spec 4.4): say so
    at = f" {d}" if d else (", observed: date unknown" if p in (_P.MEASURED, _P.CATALOG) else "")
    if is_draft(f):
        return "draft"
    if s is _S.VERIFIED:
        return f"verified{at}"
    if s is _S.OBSERVED:
        return f"catalog stats{at}" if p is _P.CATALOG else f"measured{at}"
    if s is _S.STALE:
        passed = f.evidence is not None and str(f.evidence.verdict).upper() == "PASS"
        base = "catalog stats" if p is _P.CATALOG else ("verified" if passed else "measured")
        return f"{base}{at}, stale"
    if s is _S.UNVERIFIED:
        return {
            _P.HUMAN: "human, unchecked",
            _P.DECLARED: "declared, unchecked",
            _P.CATALOG: "catalog, unchecked",
        }.get(p, "unchecked")
    return {
        _S.ESTIMATED: "estimated",
        _S.UNMEASURABLE: "unmeasurable",
        _S.CONTRADICTED: "contradicted",
        _S.WAIVED: "waived",
        _S.ERROR: "failed",
    }[s]


def _when_key(f: Fact) -> float:
    return f.observed_at.timestamp() if f.observed_at is not None else float("-inf")


def _cards_conflict(declared: str, measured: str) -> bool:
    """A declared ``1`` side measured as many (``N``/``M``), or the reverse; an unknown side
    (``?``, from a sample that saw no repeat there) never conflicts."""
    d, m = declared.split(":"), measured.split(":")
    if len(d) != 2 or len(m) != 2:
        return declared != measured
    return any("?" not in (a, b) and (a == "1") != (b == "1") for a, b in zip(d, m, strict=True))


def pick_measured(eff: Effective | None, *, allow_sampled: bool = True) -> Fact | None:
    """The newest usable exact measurement, else the newest sampled one (spec 8.5, 8.6)."""
    if eff is None:
        return None
    usable = sorted(
        (f for f in eff.all if f.provenance is _P.MEASURED and f.status not in _UNUSABLE),
        key=lambda f: (-_when_key(f), f.id),
    )
    exact = [
        f for f in usable if f.status is not _S.ESTIMATED and method_class(f.method) == "exact"
    ]
    if exact:
        return exact[0]
    sampled = [f for f in usable if f not in exact]
    return sampled[0] if sampled and allow_sampled else None


def _unmeasurable(eff: Effective | None) -> Fact | None:
    if eff is None:
        return None
    found = [f for f in eff.all if f.provenance is _P.MEASURED and f.status is _S.UNMEASURABLE]
    return max(found, key=lambda f: (_when_key(f), f.id)) if found else None


def _reason(f: Fact) -> str:
    raw = f.value.get("reason") or f.value.get("message") or "unknown reason"
    return str(raw).replace("_", " ")


def _waived(eff: Effective) -> str:
    for msg in eff.contradictions:
        if msg.startswith("WAIVED until") and "): " in msg:
            return msg[: msg.index("): ") + 3]
    return ""


def _wrap(pred: str) -> str:
    p = pred.strip()
    return p if p.startswith("(") and p.endswith(")") else f"({p})"


def severity(f: Fact) -> str:
    return "critical" if f.value.get("severity") == "critical" else "warn"


def pitfall_text(f: Fact, free: Callable[[str], str] | None = None) -> str:
    """``rule — fix: fix``; ``free`` masks the prose (the value policy's free-text pass)."""
    clean = free or (lambda x: x)
    fix = f.value.get("fix")
    text = clean(str(f.value.get("rule"))) + (f" — fix: {clean(str(fix))}" if fix else "")
    return text + tag(f)


def predicate_text(text: str, mode: str) -> tuple[str, int]:
    """SQL text under the value policy: ``none`` replaces every string literal with ``?``;
    ``masked`` sweeps shapes inside it (never name-masks identifiers); ``raw`` keeps it."""
    if mode == "raw":
        return text, 0
    if mode == "none":
        found = len(_LITERAL.findall(text))
        return _LITERAL.sub("?", text), found
    return sweep(text)


def outbound(text: str, mode: str) -> tuple[str, int]:
    """The last step before text leaves (spec 11): a shape sweep unless values are raw."""
    return (text, 0) if mode == "raw" else sweep(text)


# ----- analysis ------------------------------------------------------------------------------


@dataclass
class ColumnView:
    column: Column
    name: str
    notes: list[tuple[int, str]] = field(default_factory=list)
    keyed: list[tuple[int, str]] = field(default_factory=list)


@dataclass
class Analysis:
    """One table's knowledge sorted into what each profile renders."""

    title: str
    description: str | None = None
    never_query: str | None = None
    pitfalls: list[tuple[str, str]] = field(default_factory=list)  # (severity, text)
    warnings: list[str] = field(default_factory=list)
    hedges: list[str] = field(default_factory=list)
    answers: str | None = None
    rows: str | None = None
    live_rows: str | None = None
    table_lines: list[tuple[int, str]] = field(default_factory=list)
    keys: list[str] = field(default_factory=list)
    columns: list[ColumnView] = field(default_factory=list)
    keyed: list[tuple[int, str]] = field(default_factory=list)
    built_from: list[str] = field(default_factory=list)  # table-level lineage lines (LITE_UP)
    provenance: str = ""
    mode: str = "masked"
    masked: int = 0  # values and free-text hits masked while rendering (counted, never shown)

    def pitfall_lines(self) -> list[str]:
        return [
            f"Pitfall{' (critical)' if sev == 'critical' else ''}: {text}"
            for sev, text in self.pitfalls
        ]

    def all_warnings(self) -> list[str]:
        nq = [f"NEVER QUERY: {self.never_query}"] if self.never_query is not None else []
        return [*nq, *self.pitfall_lines(), *self.warnings]


_KIND_WORDS = {
    "VALUES": "values",
    "NULL_RATE": "null rate",
    "DISTINCT": "distinct count",
    "ROW_COUNT": "row count",
    "PRIMARY_KEY": "key check",
    "FRESHNESS": "freshness",
    "TYPE_NOTE": "type note",
    "JOIN": "join",
    "PII": "PII check",
    "DESCRIPTION": "description",
    "USE_CASES": "use cases",
    "ENTITY_TYPE": "entity type",
    "SOFT_DELETE": "live-rows filter",
    "PITFALL": "pitfall",
}


LINEAGE_CAP = 3


def _src(source: str) -> str:
    """A lineage source as rendered: an overlay path stays out of the view."""
    label = source_label(source)
    return "overlay" if label.startswith("overlay") else label


def _marker(source: str) -> str:
    return "parsed" if _src(source) == "view_definition" else "declared"


def _capped(items: Sequence[str], cap: int) -> str:
    more = len(items) - cap
    return ", ".join(items[:cap]) + (f" (+{more} more)" if more > 0 else "")


class _Analyser:
    def __init__(self, k: TableKnowledge) -> None:
        self.k = k
        self.cfg = k.config
        self.maxv = self.cfg.scope.max_values_shown
        self.a = Analysis(title=qname(k.ref))
        self.cols = {c.ref.column: ColumnView(c, ident(c.ref.column or "")) for c in k.columns}
        self.used: list[Fact] = []
        self.markers: set[str] = set()
        self.lineage_sources: list[str] = []
        self.mode = ValuePolicy.for_table(k.ref, self.cfg.sampling)
        self.a.mode = self.mode
        # any PII classification (measured shape, drafted or human) masks the whole column
        self.pii_cols = {
            name
            for name in self.cols
            if name is not None
            and (e := k.get("PII", name)) is not None
            and any(f.status is not _S.ERROR for f in e.all)
        }

    # -- helpers
    def free(self, text: str) -> str:
        """Free text (descriptions, pitfalls, reasons, notes) masked unless values are raw."""
        if self.mode == "raw":
            return text
        masked, hits = mask_free_text(text)
        self.a.masked += hits
        return masked

    def reason(self, f: Fact) -> str:
        """A probe's reason; it may quote a value, so ``none`` replaces quoted literals too."""
        text = self.free(_reason(f))
        if self.mode == "none":
            self.a.masked += len(_LITERAL.findall(text))
            text = _LITERAL.sub("?", text)
        return text

    def pred(self, text: str) -> str:
        """A live-rows predicate: SQL, so identifiers stay; ``none`` hides its literals."""
        out, hits = predicate_text(text, self.mode)
        self.a.masked += hits
        return out

    def label(self, eff: Effective) -> str:
        word = _KIND_WORDS.get(eff.kind.name, eff.kind.name.lower().replace("_", " "))
        subj = eff.subject
        if isinstance(subj, JoinRef):
            return "join " + " -> ".join(
                ", ".join(qname(c) for c in side) for side in (subj.left, subj.right)
            )
        if subj.column is not None:
            return f"{ident(subj.column)} {word}"
        return word

    def note(self, column: str | None, rank: int, text: str) -> None:
        if column is not None and column in self.cols:
            self.cols[column].notes.append((rank, text))

    def keyed(self, column: str | None, rank: int, text: str) -> None:
        if column is not None and column in self.cols:
            self.cols[column].keyed.append((rank, text))
        else:
            self.a.keyed.append((rank, text))

    def warn(self, text: str) -> None:
        if text not in self.a.warnings:
            self.a.warnings.append(text)

    # -- run
    def run(self) -> Analysis:
        k = self.k
        self.header()
        self.rows_and_freshness()
        self.primary_key()
        self.soft_delete()
        self.joins()
        self.lineage()
        for col in k.columns:
            name = col.ref.column
            assert name is not None
            self.column(col, name)
        self.failures_and_contradictions()
        self.hedges()
        self.provenance()
        self.a.columns = [self.cols[c.ref.column] for c in k.columns if c.ref.column in self.cols]
        return self.a

    def header(self) -> None:
        k, a = self.k, self.a
        d = k.get("DESCRIPTION")
        if d is not None and d.primary is not None:
            p = d.primary
            text = p.value.get("text")
            if p.status is _S.ERROR:
                self.warn(
                    f"description {'draft ' if is_draft(p) else ''}failed: {self.reason(p)}{tag(p)}"
                )
            elif isinstance(text, str) and text.strip():
                a.description = self.free(text.strip()) + tag(p)
                self.used.append(p)
        if a.description is None and k.table is not None and k.table.comment:
            a.description = self.free(k.table.comment.strip())
        nq = k.get("NEVER_QUERY")
        if nq is not None and nq.primary is not None and nq.primary.status is not _S.ERROR:
            a.never_query = self.free(
                str(nq.primary.value.get("reason") or "no reason given")
            ) + tag(nq.primary)
            self.used.append(nq.primary)
        pf = k.get("PITFALL")
        if pf is not None and pf.primary is not None:
            prov = pf.primary.provenance
            items = [f for f in pf.all if f.provenance is prov and f.status is not _S.ERROR]
            items.sort(
                key=lambda f: (f.value.get("severity") != "critical", str(f.value.get("rule")))
            )
            for f in items:
                a.pitfalls.append((severity(f), pitfall_text(f, self.free)))
                self.used.append(f)
        uc = k.get("USE_CASES")
        if uc is not None and uc.primary is not None and uc.primary.status is not _S.ERROR:
            qs = uc.primary.value.get("questions")
            if isinstance(qs, list) and qs:
                label = "Answers (draft)" if is_draft(uc.primary) else "Answers"
                a.answers = f"{label}: " + "; ".join(self.free(str(q)) for q in qs)
                self.used.append(uc.primary)

    def soft_delete(self) -> None:
        eff = self.k.get("SOFT_DELETE")
        if eff is None or eff.primary is None or eff.primary.status is _S.ERROR:
            return
        f = eff.primary
        pred = f.value.get("predicate")
        if not isinstance(pred, str) or not pred.strip():
            return
        self.used.append(f)
        if f.status in (_S.CONTRADICTED, _S.WAIVED):
            # a human/declared filter that does not fit the catalog or the measured values
            why = next((m.split(MISMATCH, 1)[1] for m in eff.contradictions if MISMATCH in m), None)
            if why is not None:
                who = "human" if f.provenance is _P.HUMAN else "declared"
                self.warn(
                    f"{_waived(eff)}live-rows filter {_wrap(self.pred(pred))} does not match "
                    f"the data: {why} ({who}) — not applied"
                )
            else:
                self.warn(
                    f"{_waived(eff)}live-rows filter {_wrap(self.pred(pred))} failed its check "
                    f"({phrase(f)}, {day(f.observed_at)}){tag(f)}"
                )
            self.keyed(None, LITE_UP, f"SOFT_DELETE {_wrap(self.pred(pred))} contradicted")
            return
        how = phrase(f)
        text = f"live rows: {_wrap(self.pred(pred))} ({how})"
        self.a.live_rows = text
        word = "(draft)" if is_draft(f) else how.replace(", ", " ")
        self.keyed(None, LITE_UP, f"SOFT_DELETE {_wrap(self.pred(pred))} {word}")
        markers = f.value.get("marker_columns")
        names = [str(m) for m in markers] if isinstance(markers, list) else []
        present = [n for n in names if n in self.cols]
        if present:
            self.markers.update(present)
            self.note(present[0], LITE_UP, text + self.marker_values(present[0]))
        else:
            self.a.table_lines.append((LITE_UP, text))

    def marker_values(self, name: str) -> str:
        """``; holds TRUE (300 rows) or NULL (9,700 rows), never FALSE (measured ...)``."""
        col = self.cols[name].column
        if col.family != "boolean" or self.mode == "none":
            return ""  # a hidden-values policy shows no values, not even TRUE/FALSE
        vals = pick_measured(self.k.get("VALUES", name), allow_sampled=False)
        nulls = pick_measured(self.k.get("NULL_RATE", name), allow_sampled=False)
        if vals is None or nulls is None or not isinstance(vals.value.get("observed"), list):
            return ""
        seen: list[str] = []
        present: set[object] = set()
        for item in vals.value["observed"]:
            if isinstance(item, dict) and isinstance(item.get("value"), bool):
                n = item.get("count")
                present.add(item["value"])
                seen.append(
                    self.shown(name, item["value"])
                    + (f" ({rows(n)})" if isinstance(n, int) else "")
                )
        rows_n, non_null = nulls.value.get("rows"), nulls.value.get("non_null")
        rate = nulls.value.get("rate")
        if isinstance(rows_n, int) and isinstance(non_null, int) and rows_n > non_null:
            seen.append(f"NULL ({rows(rows_n - non_null)})")
        elif isinstance(rate, (int, float)) and rate > 0:
            seen.append("NULL")
        never = [self.shown(name, b) for b in (True, False) if b not in present]
        if not seen:
            return ""
        self.used += [vals, nulls]
        tail = f", never {' or '.join(never)}" if never else ""
        return f"; holds {' or '.join(seen)}{tail} ({phrase(vals)})"

    def primary_key(self) -> None:
        k = self.k
        eff = k.get("PRIMARY_KEY")
        declared = [c for c in k.constraints if c.kind == "primary_key"]
        m = pick_measured(eff)
        if eff is not None and m is not None:
            # several key candidates may be measured; the declared key's own check comes first
            want = (
                list(declared[0].columns)
                if declared
                else (eff.expectation.value.get("columns") if eff.expectation else None)
            )
            same = [
                f
                for f in eff.all
                if f.provenance is _P.MEASURED
                and f.status not in _UNUSABLE
                and f.value.get("columns") == want
            ]
            if want and same:
                m = pick_measured(replace(eff, all=tuple(same)))
        cols: tuple[str, ...] = ()
        how: str | None = None
        if m is not None and isinstance(m.value.get("columns"), list):
            self.used.append(m)
            cols = tuple(str(c) for c in m.value["columns"])
            unique = m.value.get("unique")
            if unique is True:
                how = f"unique, {phrase(m)}"
            elif unique is False:
                dup = m.value.get("duplicate_keys")
                n = f"{count(dup)} duplicate keys" if isinstance(dup, int) else "duplicates"
                self.warn(f"PK ({', '.join(map(ident, cols))}) is not unique: {n} ({phrase(m)})")
                how = "NOT unique, see warning"
            elif m.value.get("consistent_with_sample"):
                frac = _fraction(fraction_of(m))
                how = "no duplicate in sample" + (f" (sampled {pct(frac)})" if frac else "")
            else:
                how = "uniqueness unknown"
            if m.value.get("complete") is False:
                self.warn(f"PK ({', '.join(map(ident, cols))}) has rows with a NULL key part")
        elif declared:
            cols = declared[0].columns
            how = "declared, enforced" if declared[0].enforced else "declared, unchecked"
        elif eff is not None and eff.expectation is not None:
            raw = eff.expectation.value.get("columns")
            if isinstance(raw, list):
                cols = tuple(str(c) for c in raw)
                how = phrase(eff.expectation)
        if not cols or how is None:
            return
        shown = ", ".join(map(ident, cols))
        self.a.keys.append(f"PK {shown}" if len(cols) == 1 else f"PK ({shown})")
        self.keyed(None, LITE_UP, f"PK {shown} {how.replace(', ', ' ')}")
        if len(cols) == 1 and cols[0] in self.cols:
            self.note(cols[0], LITE_UP, f"PK ({how})")
        else:
            self.a.table_lines.append((LITE_UP, f"PK ({shown}): {how}"))

    def rows_and_freshness(self) -> None:
        k = self.k
        eff = k.get("ROW_COUNT")
        p = eff.primary if eff is not None else None
        n = None if p is None else p.value.get("rows")
        if p is not None and isinstance(n, int) and p.status is not _S.ERROR:
            self.a.rows = f"{rows(n)} ({phrase(p)})"
            self.keyed(None, STANDARD_UP, f"ROWS {n} {phrase(p)}")
            self.used.append(p)
        elif (u := _unmeasurable(eff)) is not None:
            self.a.rows = f"rows: unmeasurable ({self.reason(u)})"
            self.keyed(None, STANDARD_UP, "ROWS unmeasurable")
        elif k.table is not None and k.table.row_count is not None:
            self.a.rows = f"{rows(k.table.row_count)} (catalog stats)"
            self.keyed(None, STANDARD_UP, f"ROWS {k.table.row_count} catalog stats")
        fr = k.get("FRESHNESS")
        if fr is None:
            return
        newest: dict[str, Fact] = {}
        for f in fr.all:
            col = f.value.get("column")
            usable = f.provenance is _P.MEASURED and f.status not in _UNUSABLE
            if (
                usable
                and isinstance(col, str)
                and (col not in newest or _when_key(f) > _when_key(newest[col]))
            ):
                newest[col] = f
        for col in sorted(newest):
            f = newest[col]
            age = f.value.get("age_seconds")
            mv = f.value.get("max_value")
            if self.mode == "none":
                newest_text, newest_key = "", "hidden"
            else:
                shown = self.shown(col, mv)
                plain = str(mv) if shown == quote(mv) else shown  # a timestamp needs no quotes
                newest_text, newest_key = f": {plain}", plain
            age_text = (
                f", age {sig4(float(age) / 86400)} days" if isinstance(age, (int, float)) else ""
            )
            self.a.table_lines.append(
                (
                    FULL_ONLY,
                    f"newest {ident(col)}{newest_text}{age_text} ({phrase(f)})",
                )
            )
            self.keyed(None, FULL_ONLY, f"FRESH {ident(col)} max={newest_key} {phrase(f)}")
            self.used.append(f)

    def joins(self) -> None:
        k, g = self.k, self.cfg.graph
        seen: set[tuple[tuple[str, ...], tuple[str, ...]]] = set()
        inbound: dict[str, list[str]] = {}
        for eff in k.joins():
            join = eff.subject
            assert isinstance(join, JoinRef)
            key = (
                tuple(c.canonical() for c in join.left),
                tuple(c.canonical() for c in join.right),
            )
            seen.add(key)
            outgoing = join.left[0].table_ref() == k.ref
            if outgoing:
                self.outgoing(eff, join)
            if join.right[0].table_ref() == k.ref and len(join.right) == 1:
                m = pick_measured(eff)
                rate = None if m is None else m.value.get("match_rate")
                if (
                    rate is not None
                    and join_verdict(rate, g.ok_threshold, g.weak_threshold) == "BROKEN"
                ):
                    continue
                cov = None if m is None else m.value.get("coverage")
                src = ", ".join(qname(c) for c in join.left)
                text = f"← {src}"
                if m is not None and isinstance(cov, (int, float)):
                    # a measured number always says how it is known (spec 2 G6); a sampled
                    # one says so inside join_number
                    sampled = m.status is _S.ESTIMATED or method_class(m.method) == "sampled"
                    how = "" if sampled else f", {phrase(m)}"
                    text += f" (coverage {join_number(m, 'coverage')}{how})"
                col = join.right[0].column
                assert col is not None
                inbound.setdefault(col, []).append(text)
        for col, items in inbound.items():
            self.note(col, FULL_ONLY, capped(items, INBOUND_SHOWN, ", "))
        for con in k.constraints:
            if con.kind != "foreign_key" or con.ref_table is None or con.ref_columns is None:
                continue
            rt = con.ref_table
            left = tuple(k.column_ref(c).canonical() for c in con.columns)
            right = tuple(
                ObjectRef(rt.catalog, rt.schema, rt.table, c).canonical() for c in con.ref_columns
            )
            if (left, right) in seen:
                continue
            target = ", ".join(
                qname(ObjectRef(rt.catalog, rt.schema, rt.table, c)) for c in con.ref_columns
            )
            text = f"→ {target} (declared FK, not measured)"
            lcols = ", ".join(map(ident, con.columns))
            self.a.keys.append(f"{lcols} → {target} (declared FK)")
            self.keyed(
                con.columns[0] if len(con.columns) == 1 else None,
                LITE_UP,
                f"JOIN {lcols} -> {target} declared",
            )
            if len(con.columns) == 1:
                self.note(con.columns[0], LITE_UP, text)
            else:
                self.a.table_lines.append((LITE_UP, f"({lcols}) {text}"))

    def outgoing(self, eff: Effective, join: JoinRef) -> None:
        g = self.cfg.graph
        target = ", ".join(qname(c) for c in join.right)
        names = [c.column or "" for c in join.left]
        lcols = ", ".join(map(ident, names))
        col = names[0] if len(names) == 1 else None
        lead = ident(names[0]) if col is not None else f"({lcols})"
        cast = f" (cast to {join.cast_to})" if join.cast_to else ""
        m = pick_measured(eff)
        exp = eff.expectation
        if m is None:
            p = eff.primary
            if p is None or p.status is _S.ERROR:
                return
            u = _unmeasurable(eff)
            if u is not None:
                how = f"unmeasurable: {self.reason(u)}"
            elif p.provenance is _P.DRAFTED:
                how = "candidate, not measured"
            else:
                how = f"{phrase(p)}, not measured"
            self.used.append(p)
            text = f"→ {target}{cast} ({how}){tag(p)}"
            self.a.keys.append(f"{lead} → {target} ({how}){tag(p)}")
            self.keyed(col, LITE_UP, f"JOIN {lcols} -> {target} {how.replace(', ', ' ')}{tag(p)}")
        else:
            self.used.append(m)
            v = m.value
            rate, cov, card = v.get("match_rate"), v.get("coverage"), v.get("cardinality")
            verdict = None if rate is None else join_verdict(rate, g.ok_threshold, g.weak_threshold)
            sampled = m.status is _S.ESTIMATED or method_class(m.method) == "sampled"

            when = "" if sampled else f" ({phrase(m)})"
            if verdict == "BROKEN":
                self.warn(
                    f"{lead} → {target} BROKEN: match {join_number(m, 'match_rate')}{when}"
                    " — do not join on it"
                )
                text = f"→ {target} BROKEN (see warning)"
                shown = "unknown" if rate is None else plain4(rate)
                self.keyed(col, LITE_UP, f"JOIN {lcols} -> {target} BROKEN match={shown}")
            else:
                parts = [f"→ {target}{cast}" + (f" {card}" if card else "")]
                parts.append(f"match {join_number(m, 'match_rate')}")
                if isinstance(cov, (int, float)) and cov < g.ok_threshold:
                    parts.append(f"coverage {join_number(m, 'coverage')}")
                text = ", ".join(parts) + when
                if verdict == "WEAK":
                    self.warn(
                        f"{lead} → {target} WEAK: match {join_number(m, 'match_rate')}{when}"
                        " — expect unmatched rows"
                    )
                bits = [str(card)] if card else []
                if rate is not None:
                    bits.append(f"match {join_number(m, 'match_rate')}")
                short = f" ({', '.join(bits)})" if bits else ""
                self.a.keys.append(f"{lead} → {target}{short}")
                approx = "≈" if sampled else ""
                nums = f" match={approx}{plain4(rate)}" if isinstance(rate, (int, float)) else ""
                nums += f" coverage={approx}{plain4(cov)}" if isinstance(cov, (int, float)) else ""
                how = "sampled" if sampled else phrase(m)
                self.keyed(
                    col,
                    LITE_UP,
                    f"JOIN {lcols} -> {target}{' ' + card if card else ''}{nums} {how}",
                )
            ecard = None if exp is None else exp.value.get("cardinality")
            if isinstance(ecard, str) and isinstance(card, str) and _cards_conflict(ecard, card):
                self.warn(f"{lead} → {target}: declared {ecard} but measured {card} ({phrase(m)})")
        if col is not None:
            self.note(col, LITE_UP, text)
        else:
            self.a.table_lines.append((LITE_UP, f"{lead} {text}"))

    def column(self, col: Column, name: str) -> None:
        k = self.k
        pii = k.get("PII", name)
        if pii is not None and pii.primary is not None and pii.primary.status is not _S.ERROR:
            cat = pii.primary.value.get("category")
            # first on the line: the agent learns the column is personal before any value
            self.cols[name].notes.insert(0, (LITE_UP, f"PII {cat}{tag(pii.primary)}"))
            self.keyed(name, LITE_UP, f"PII {ident(name)} category={cat}{tag(pii.primary)}")
            self.used.append(pii.primary)
        if name not in self.markers:
            self.values(name)
            self.nulls(name)
        self.type_note(col, name)
        dist = pick_measured(k.get("DISTINCT", name))
        if dist is not None:
            est = dist.value.get("estimate")
            if is_estimate(dist) and isinstance(est, Mapping):
                self.note(
                    name, FULL_ONLY, "distinct " + estimate_text(est, fraction_of(dist), rate=False)
                )
                self.used.append(dist)
            elif isinstance(dist.value.get("distinct_keys"), int):
                n = dist.value["distinct_keys"]
                self.note(name, FULL_ONLY, f"distinct {count(n)} ({phrase(dist)})")
                self.keyed(name, FULL_ONLY, f"DISTINCT {ident(name)} {n} {phrase(dist)}")
                self.used.append(dist)
        d = k.get("DESCRIPTION", name)
        if d is not None and d.primary is not None:
            p = d.primary
            if p.status is _S.ERROR:
                draft = "draft " if is_draft(p) else ""
                self.warn(f"{ident(name)} description {draft}failed: {self.reason(p)}{tag(p)}")
            elif isinstance(p.value.get("text"), str):
                self.note(name, FULL_ONLY, self.free(str(p.value["text"]).strip()) + tag(p))
                self.used.append(p)
        elif col.comment:
            self.note(name, FULL_ONLY, self.free(col.comment.strip()))

    def values(self, name: str) -> None:
        """Declared and observed values, through the value policy (spec 11).

        ``none`` shows counts only; ``masked`` masks every value of a PII-tagged column and every
        detector hit elsewhere; ``raw`` (an explicit opt-in) shows values as stored.
        """
        eff = self.k.get("VALUES", name)
        if eff is None:
            return
        exp = eff.expectation
        q = ident(name)
        hidden = self.mode == "none"
        if exp is not None and isinstance(exp.value.get("values"), list):
            self.used.append(exp)
            raw_declared = [str(v) for v in exp.value["values"]]
            declared = [self.shown(name, v) for v in raw_declared]
            shown = capped(declared, self.maxv)
            extras = self.extras(eff, raw_declared, bool(exp.value.get("case_sensitive", True)))
            text = (
                f"values: {len(raw_declared)} declared {_HIDDEN}" if hidden else f"values: {shown}"
            )
            if extras and hidden:
                n_rows = sum(n for _, n, _ in extras if n is not None)
                text += f" (+ {len(extras)} more observed)"
                self.warn(
                    f"{_waived(eff)}{q}: {len(raw_declared)} declared values — observed also "
                    f"{len(extras)} undeclared value(s) ({rows(n_rows)}) {_HIDDEN}"
                )
            elif extras:
                text += (
                    " (+ observed "
                    + capped([self.shown(name, v) for v, _, _ in extras], self.maxv, ", ")
                    + ")"
                )
                rendered = [
                    self.shown(name, v)
                    + (f" ({rows(n)})" if n is not None else "")
                    + ("" if ex else " (seen in sample)")
                    for v, n, ex in extras
                ]
                also = capped(rendered, self.maxv, ", ")
                self.warn(f"{_waived(eff)}{q}: declared values {shown} — observed also {also}")
            folded = {d.casefold(): d for d in raw_declared}
            for v, _, _ in extras:
                d = folded.get(v.casefold())
                if d is not None and d != v:
                    self.warn(f"{q}: case drift: {self.pair(name, [d, v])}{_CASE_DRIFT}")
            self.note(name, STANDARD_UP, text)
            if hidden:
                keyed = f"VALUES {q} declared_count={len(raw_declared)}"
                keyed += f" observed_extra_count={len(extras)}" if extras else ""
                keyed += " hidden"
            else:
                keyed = f"VALUES {q} declared={','.join(declared)}"
                if extras:
                    keyed += " observed_extra=" + ",".join(
                        f"{self.shown(name, v)}:{n}" for v, n, _ in extras
                    )
            self.keyed(name, STANDARD_UP, keyed)
            return
        m = pick_measured(eff)
        if m is None or not isinstance(m.value.get("observed"), list):
            return
        self.used.append(m)
        items = [i for i in m.value["observed"] if isinstance(i, dict)]
        sampled = m.status is _S.ESTIMATED or method_class(m.method) == "sampled"
        if sampled:
            frac = _fraction(fraction_of(m))
            how = f"sampled {pct(frac)}" if frac is not None else "sampled"
            if hidden:
                self.note(
                    name, STANDARD_UP, f"values: {len(items)} seen in sample {_HIDDEN} ({how})"
                )
                self.keyed(
                    name, STANDARD_UP, f"VALUES {q} seen_in_sample_count={len(items)} hidden"
                )
            else:
                vals = [self.shown(name, i.get("value")) for i in items]
                self.note(
                    name,
                    STANDARD_UP,
                    f"values seen in sample: {capped(vals, self.maxv, ', ')} "
                    f"({how}; rarer values may be missing)",
                )
                self.keyed(name, STANDARD_UP, f"VALUES {q} seen_in_sample={','.join(vals)}")
        else:
            distinct = m.value.get("distinct")
            more = (
                distinct - len(items) if isinstance(distinct, int) and distinct > len(items) else 0
            )
            if hidden:
                n = distinct if isinstance(distinct, int) else len(items)
                self.note(name, STANDARD_UP, f"values: {count(n)} distinct {_HIDDEN} ({phrase(m)})")
                self.keyed(name, STANDARD_UP, f"VALUES {q} distinct={n} hidden {phrase(m)}")
            else:
                rendered = [
                    self.shown(name, i.get("value"))
                    + (f" ({count(i['count'])})" if isinstance(i.get("count"), int) else "")
                    for i in items
                ]
                text = "values: " + capped(rendered, self.maxv, ", ")
                if more and len(items) <= self.maxv:
                    text += f" +{more} more"
                self.note(name, STANDARD_UP, f"{text} ({phrase(m)})")
                pairs = ",".join(
                    f"{self.shown(name, i.get('value'))}:{i.get('count')}" for i in items
                )
                self.keyed(name, STANDARD_UP, f"VALUES {q} observed={pairs} {phrase(m)}")
        groups: dict[str, list[str]] = {}
        for i in items:
            val = i.get("value")
            if isinstance(val, str):
                groups.setdefault(val.casefold(), []).append(val)
        for vs in groups.values():
            if len(set(vs)) > 1:
                self.warn(f"{q}: case drift: {self.pair(name, vs)}{_CASE_DRIFT}")

    def shown(self, column: str, value: object) -> str:
        """One stored value as the agent may see it (spec 11), quoted like SQL text.

        ``masked``: every value of a PII-tagged column becomes a shape token (``<email>``, or
        ``<masked>`` when no shape matched); elsewhere a detector hit is masked in place.
        ``none``: ``<hidden>`` (callers show counts instead). ``raw``: as stored.
        """
        if value is None:
            return "NULL"
        if self.mode == "none":
            self.a.masked += 1
            return "<hidden>"
        if self.mode == "raw":
            return quote(value)
        if isinstance(value, str) and TOKEN.match(value):
            return value  # masked at storage already
        masked, category = mask_value(value)
        whole = bool(TOKEN.match(masked))
        if column in self.pii_cols:
            # a personal column shows a whole shape token or nothing: a partial hit
            # ("Zebulon Quartzfeld <email>") would still leak the rest of the value
            self.a.masked += 1
            return masked if category is not None and whole else "<masked>"
        if category is None:
            return quote(value)
        self.a.masked += 1
        return masked if whole else quote(masked)

    def pair(self, column: str, values: Sequence[str]) -> str:
        """Values that differ only in case; masked or hidden ones are counted, not shown."""
        shown = [self.shown(column, v) for v in values]
        if self.mode == "none" or len(set(shown)) < len(set(values)):
            return f"{len(set(values))} values (values hidden by policy)"
        return "/".join(shown)

    def extras(
        self, eff: Effective, declared: Sequence[str], case_sensitive: bool
    ) -> list[tuple[str, int | None, bool]]:
        """Observed values outside the declaration, over the non-superseded measurements."""
        ms = sorted(
            (f for f in eff.all if f.provenance is _P.MEASURED and f.status not in _UNUSABLE),
            key=lambda f: (-_when_key(f), f.id),
        )
        pivot = next(
            (f for f in ms if f.status is not _S.ESTIMATED and method_class(f.method) == "exact"),
            None,
        )
        if pivot is not None:
            ms = [f for f in ms if _when_key(f) >= _when_key(pivot)]

        def fold(s: str) -> str:
            return s if case_sensitive else s.casefold()

        known = {fold(d) for d in declared}
        out: dict[str, tuple[int | None, bool]] = {}
        for f in ms:
            obs = f.value.get("observed")
            if not isinstance(obs, list):
                continue
            exact = f.status is not _S.ESTIMATED and method_class(f.method) == "exact"
            for item in obs:
                if not isinstance(item, dict) or fold(str(item.get("value"))) in known:
                    continue
                v, n = str(item.get("value")), item.get("count")
                n = n if isinstance(n, int) else None
                old_n, old_ex = out.get(v, (None, False))
                best = max((x for x in (old_n, n) if x is not None), default=None)
                out[v] = (best, old_ex or exact)
                self.used.append(f)
        return sorted(
            ((v, n, ex) for v, (n, ex) in out.items()), key=lambda t: (-(t[1] or 0), t[0])
        )

    def nulls(self, name: str) -> None:
        m = pick_measured(self.k.get("NULL_RATE", name))
        if m is None:
            return
        rate = m.value.get("rate")
        est = m.value.get("estimate")
        q = ident(name)
        if is_estimate(m) and isinstance(est, Mapping):
            self.note(name, FULL_ONLY, "null " + estimate_text(est, fraction_of(m)))
            self.used.append(m)
        elif m.value.get("dead") is True:
            self.warn(f"{q}: always NULL (dead column, {phrase(m)})")
            self.keyed(name, LITE_UP, f"NULLS {q} rate=1 dead {phrase(m)}")
            self.used.append(m)
        elif isinstance(rate, (int, float)) and rate > 0:
            self.note(name, FULL_ONLY, f"null {pct(rate)} ({phrase(m)})")
            self.keyed(name, FULL_ONLY, f"NULLS {q} rate={plain4(rate)} {phrase(m)}")
            self.used.append(m)

    def type_note(self, col: Column, name: str) -> None:
        eff = self.k.get("TYPE_NOTE", name)
        if eff is None:
            return
        q = ident(name)
        m = pick_measured(eff)
        if m is not None:
            means, rate = self.free(str(m.value.get("means"))), m.value.get("rate")
            est = m.value.get("estimate")
            self.used.append(m)
            if is_estimate(m) and isinstance(est, Mapping):
                self.note(
                    name,
                    STANDARD_UP,
                    f"looks like {means}: {estimate_text(est, fraction_of(m))} of values",
                )
            elif m.value.get("holds") is True and isinstance(rate, (int, float)):
                self.warn(
                    f"{q}: stored as {col.native_type}, holds {means} ({pct(rate)} of values, "
                    f"{phrase(m)}) — CAST before comparing"
                )
                held = "text holding" if col.family == "string" else "holds"
                self.note(name, LITE_UP, f"{held} {means} (see warning)")
                self.keyed(
                    name,
                    LITE_UP,
                    f"TYPE {q} stored_as={col.native_type} means={means} rate={plain4(rate)} "
                    f"{phrase(m)}",
                )
            elif isinstance(rate, (int, float)):
                self.note(name, FULL_ONLY, f"{pct(rate)} of values look like {means} ({phrase(m)})")
        for f in eff.all:
            if f.provenance is _P.MEASURED or f.status is _S.ERROR:
                continue
            raw_means, stored = f.value.get("means"), f.value.get("stored_as")
            note = None if raw_means is None else self.free(str(raw_means))
            if f.provenance is _P.DRAFTED and stored:
                how = "" if is_draft(f) else f" ({phrase(f)})"
                self.warn(f"{q}: stored as {stored}, holds {note}{how}{tag(f)}")
                self.used.append(f)
            elif note:
                stored_t = f", stored as {stored}" if stored else ""
                self.note(name, STANDARD_UP, f"note: {note}{stored_t} ({phrase(f)})")
                self.keyed(name, STANDARD_UP, f"NOTE {q} {note}")
                self.used.append(f)

    def lineage(self) -> None:
        """Parsed or declared lineage, always marked unverified (spec 4.6). Lines name the
        sources; they never say measured or verified."""
        k, a = self.k, self.a
        own = k.ref.canonical()
        columns = {k.column_ref(n).canonical(): n for n in self.cols if n is not None}
        notes: dict[str, list[str]] = {}
        sources: set[str] = set()
        for link in k.lineage.links:
            if not (link.inputs or link.unresolved):
                continue
            refs = [*link.inputs, *(f"{u} (not in catalog)" for u in link.unresolved)]
            keyed = [*link.inputs, *(f"{u}(unresolved)" for u in link.unresolved)]
            if link.level == "table":
                if link.subject != own:
                    continue
                src = ", ".join(f"{_src(s)} ({_marker(s)})" for s in link.sources)
                a.built_from.append(
                    f"Built from: {_capped(refs, LINEAGE_CAP)} · {src} · unverified"
                )
                self.keyed(None, LITE_UP, f"LINEAGE {a.title} <- {', '.join(keyed)}")
            else:
                col = columns.get(link.subject)
                if col is None:
                    continue
                how = f" ({link.transform})" if link.transform else ""
                notes.setdefault(col, []).append(f"{_capped(refs, LINEAGE_CAP)}{how}")
                tr = f" {link.transform}" if link.transform else ""
                text = f"COLUMN_LINEAGE {a.title}.{ident(col)} <- {', '.join(keyed)}{tr}"
                self.keyed(col, STANDARD_UP, text)
            sources.update(_src(s) for s in link.sources)
        for col, parts in notes.items():
            self.note(col, STANDARD_UP, "← " + "; ".join(parts))
        self.lineage_sources = sorted(sources)

    def failures_and_contradictions(self) -> None:
        # stated elsewhere in the block (values, measurements, the live-rows filter line)
        skip = ("latest measurement failed", "measured contradiction", "declared values", MISMATCH)
        for key in sorted(self.k.effective):
            eff = self.k.effective[key]
            subj = eff.subject
            if isinstance(subj, JoinRef) and subj.left[0].table_ref() != self.k.ref:
                continue  # an inbound join's problems belong to the child table's block
            label = self.label(eff)
            good = pick_measured(eff)
            for f in eff.all:
                failed = f.status is _S.ERROR and f.provenance is _P.MEASURED
                if failed and (good is None or _when_key(f) > _when_key(good)):
                    self.warn(
                        f"{label}: measurement failed ({self.reason(f)}, {day(f.observed_at)})"
                    )
            p = eff.primary
            if (
                p is not None
                and p.status is _S.ERROR
                and p.provenance is not _P.MEASURED
                and eff.kind.name != "DESCRIPTION"
            ):
                self.warn(
                    f"{label} {'draft ' if is_draft(p) else ''}failed: {self.reason(p)}{tag(p)}"
                )
            for f in eff.all:
                if f.status not in (_S.CONTRADICTED, _S.WAIVED) or eff.kind.name == "SOFT_DELETE":
                    continue
                if f.provenance is _P.MEASURED:
                    if eff.kind.name == "VALUES":
                        continue
                    sample = " (sampled)" if method_class(f.method) == "sampled" else ""
                    at = day(f.observed_at)
                    self.warn(f"{_waived(eff)}{label}: measured contradiction{sample} ({at})")
                elif eff.kind.name != "JOIN" or f.status is _S.WAIVED:
                    who = "drafted" if is_draft(f) else phrase(f)
                    self.warn(f"{_waived(eff)}{label}: {who} claim failed its check{tag(f)}")
                else:
                    self.warn(f"{label}: drafted join failed its check{tag(f)}")
            for msg in eff.contradictions:
                core = msg[len(_waived(eff)) :] if msg.startswith("WAIVED until") else msg
                if core.startswith(skip) or "fact failed its check" in core:
                    continue
                self.warn(f"{label}: {msg}")

    def hedges(self) -> None:
        if not self.k.measured():
            self.a.hedges.append(
                "unmeasured: nothing here has been measured yet; figures are declared or drafted"
            )
        if any(is_estimate(f) for f in self.used):
            self.a.hedges.append("sampled table: ≈ and ≥ figures are estimates from a sample")

    def provenance(self) -> None:
        used = list({f.id: f for f in self.used}.values())
        parts: list[str] = []
        if any(is_draft(f) for f in used):
            parts.append("drafted text marked (draft) is unchecked")
        if any(f.provenance is _P.HUMAN for f in used):
            parts.append("human overlay")
        if any(f.provenance is _P.DECLARED for f in used):
            parts.append("declared (dbt)")
        days = sorted(
            {
                d
                for f in used
                if f.provenance is _P.MEASURED
                and not is_estimate(f)
                and (d := day(f.observed_at)) is not None
            }
        )
        if days:
            parts.append(
                f"measured {days[0]}" if len(days) == 1 else f"measured {days[0]} to {days[-1]}"
            )
        fracs = sorted(
            {fr for f in used if is_estimate(f) and (fr := _fraction(fraction_of(f))) is not None}
        )
        if fracs:
            parts.append("sampled " + ", ".join(pct(fr) for fr in fracs))
        if self.lineage_sources:
            parts.append(f"lineage {', '.join(self.lineage_sources)} (unverified)")
        if not parts:
            parts.append("catalog only")
        self.a.provenance = "Provenance: " + " · ".join(parts)


def analyse(k: TableKnowledge) -> Analysis:
    """Sort one table's effective knowledge into lines (deterministic)."""
    return _Analyser(k).run()


# ----- the agent profile ---------------------------------------------------------------------


def name_only(a: Analysis) -> str:
    head = f"[{a.title}]"
    if a.never_query is not None:
        return f"{head} {WARN} NEVER QUERY: {a.never_query}"
    n = len(a.pitfalls) + len(a.warnings)
    if a.description is not None:
        head += f" {a.description}"
    if n:
        head += f" ({n} warning{'s' if n != 1 else ''} not shown; describe the table)"
    return head


def warning_lines(a: Analysis) -> list[str]:
    return [f"  {WARN} {w}" for w in a.all_warnings()]


def shown_columns(
    k: TableKnowledge, a: Analysis, level: DetailLevel
) -> tuple[list[ColumnView], int]:
    """Columns at ``level`` (LITE: the priority subset) and how many were left out."""
    views = a.columns
    if level is DetailLevel.LITE:
        keep = lite_columns(k.columns, k.constraints, k.facts, limit=LITE_MAX_COLUMNS)
        kept = {c.ref.column for c in keep}
        chosen = [v for v in views if v.column.ref.column in kept]  # shown in catalog order
        return chosen, len(views) - len(chosen)
    return list(views), 0


def render_agent(k: TableKnowledge, level: DetailLevel) -> str:
    """The agent (executor-style) table block at ``level`` (spec 13.3)."""
    a = analyse(k)
    if level is DetailLevel.NAME_ONLY:
        return name_only(a)
    r = RANK[level]
    lines = [f"[{a.title}]" + (f" {a.description}" if a.description else "")]
    lines += warning_lines(a)
    lines += [f"  {b}" for b in a.built_from]
    if a.answers and r <= STANDARD_UP:
        lines.append(f"  {a.answers}")
    lines += [f"  {h}" for h in a.hedges]
    if a.rows and r <= STANDARD_UP:
        lines.append(f"  {a.rows}")
    lines += [f"  {t}" for rk, t in a.table_lines if r <= rk]
    cols, hidden = shown_columns(k, a, level)
    if cols:
        lines.append("  Columns:")
        w = max(len(v.name) for v in cols)
        tw = max(len(v.column.native_type) for v in cols)
        for v in cols:
            notes = "; ".join(t for rk, t in v.notes if r <= rk)
            lines.append(f"    {v.name:<{w}}  {v.column.native_type:<{tw}}  {notes}".rstrip())
    if hidden:
        lines.append(f"  +{hidden} more columns")
    if r <= STANDARD_UP:
        lines.append(f"  {a.provenance}")
    return "\n".join(lines)


# ----- join paths ----------------------------------------------------------------------------


def path_aliases(tables: Sequence[ObjectRef]) -> list[str]:
    """One alias per table occurrence: the bare name, ``name_<n>`` for a revisit or a clash."""
    out: list[str] = []
    taken: set[str] = set()
    seen: dict[ObjectRef, int] = {}
    for t in tables:
        n = seen.get(t, 0) + 1
        seen[t] = n
        i = n
        alias = t.table if n == 1 else f"{t.table}_{n}"
        while alias in taken:
            i += 1
            alias = f"{t.table}_{i}"
        taken.add(alias)
        out.append(alias)
    return out


def _side(alias: str, cols: Iterable[ObjectRef]) -> str:
    parts = [f"{ident(alias)}.{ident(c.column or '')}" for c in cols]
    return parts[0] if len(parts) == 1 else "(" + ", ".join(parts) + ")"


def render_path(path: Path, *, snippet: str | None = None) -> list[str]:
    """``shop.a → shop.b · support 98%`` and one ``a.x = b.y (N:1)`` line per hop.

    A revisited table gets an alias (``shop.orders AS orders_2``); a provisional route names its
    narrowest support as a number; a sampled hop is marked ``≈``.
    """
    tables = list(path.tables)
    aliases = path_aliases(tables)
    names = [
        qname(t) + (f" AS {ident(al)}" if al != t.table else "")
        for t, al in zip(tables, aliases, strict=True)
    ]
    # Path.coverage is the narrowest *directional* support over the hops (match rate one way,
    # reverse coverage the other), so it is called support here, never coverage.
    sup = "unknown" if path.coverage is None else pct(path.coverage)
    if path.estimated and path.coverage is not None:
        sup = f"≈ {sup}"
    status = f"provisional: narrowest support {sup}" if path.provisional else f"support {sup}"
    head = "  " + " → ".join(names) + f" · {status}" + (" · sampled" if path.estimated else "")
    lines = [head]
    for i, hop in enumerate(path.hops):
        join = hop.edge.join
        start, end = aliases[i], aliases[i + 1]
        child, parent = (end, start) if hop.reverse else (start, end)
        card = hop.edge.value.get("cardinality") if hop.edge.measured else None
        extra = [str(card)] if card else []
        if join.cast_to:
            extra.append(f"cast to {join.cast_to}")
        if not hop.edge.measured:
            extra.append("unmeasured")
        tail = f" ({', '.join(extra)})" if extra else ""
        lines.append(f"    {_side(child, join.left)} = {_side(parent, join.right)}{tail}")
    if snippet:
        first, *rest = snippet.splitlines()
        lines.append(f"    SQL: {first}")
        lines += [f"         {ln}" for ln in rest]
    return lines


# ----- the value policy for other outbound surfaces ------------------------------------------

ValuePresenter = _Analyser
"""One table's value policy for surfaces other than a table block (the agent tools): ``mode``,
``shown(column, value)``, ``free(text)``, ``reason(fact)``, ``pii_cols``; ``run()`` gives the
policy-applied :class:`Analysis`. The same object the renderer uses, so they cannot drift."""
