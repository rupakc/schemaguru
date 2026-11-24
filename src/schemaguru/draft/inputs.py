"""What the LLM is shown about one table (spec 10.2) under the value policy (spec 11).

* ``masked`` (default): every value goes through :func:`schemaguru.pii.mask_value`, one value
  at a time. Free-text metadata (table and column comments) goes through
  :func:`mask_free_text`: the shape sweep plus embedded person-name masking. Then a final shape
  sweep (:func:`schemaguru.pii.sweep`) runs over every string of the assembled payload, keys
  included. Identifiers are never name-masked (that would destroy table/column names). All hits
  are counted in ``pii_sweep_hits``.
* ``raw``: values unmasked, only when the effective :class:`~schemaguru.pii.ValuePolicy` says
  so (explicit opt-in). No sweep: the user asked for the real values.
* ``none``: no values at all, only names, types, comments and statistics. Comment masking and
  the sweep still run (comments can hold personal data; stricter than the spec's masked-only
  sweep).

Values are only the *top observed values* already measured for low-cardinality columns (and a
human's declared list) plus at most ``sample_values_per_column`` caller-supplied examples per
column. Nothing here writes to the store: raw values are never persisted by drafting.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from schemaguru._util.hashing import canonical_json
from schemaguru.config import Config
from schemaguru.model.facts import Fact, Provenance, Status
from schemaguru.model.joins import JoinRef
from schemaguru.model.refs import ObjectRef
from schemaguru.pii import ValuePolicy, mask_value, sweep
from schemaguru.pii.detectors import name_spans, names_enabled
from schemaguru.pii.policy import Mode
from schemaguru.store.protocol import Store

_STAT_KINDS = ("NULL_RATE", "DISTINCT", "VALUES", "ROW_COUNT")
_USABLE = (Status.OBSERVED, Status.VERIFIED, Status.ESTIMATED, Status.STALE, Status.UNVERIFIED)


@dataclass(frozen=True)
class DraftInput:
    """The outbound description of one table; ``payload`` is already masked and swept."""

    table: ObjectRef
    mode: Mode
    payload: dict[str, Any]
    pii_sweep_hits: int
    masked: tuple[tuple[str, str], ...]  # (column, category) whose values were masked

    def text(self) -> str:
        return canonical_json(self.payload)


def _newest(facts: Sequence[Fact], prov: Provenance | None = None) -> Fact | None:
    cands = [f for f in facts if f.status in _USABLE and (prov is None or f.provenance is prov)]
    if not cands:
        return None
    return max(cands, key=lambda f: (f.observed_at is not None, f.observed_at or 0, f.id))


class _Values:
    def __init__(self, mode: Mode) -> None:
        self.mode = mode
        self.masked: set[tuple[str, str]] = set()

    def one(self, column: str, value: object) -> str | None:
        if value is None:
            return None
        if self.mode == "raw":
            return value if isinstance(value, str) else str(value)
        text, cat = mask_value(value)
        if cat is not None:
            self.masked.add((column, cat))
        return text


def mask_free_text(text: str) -> tuple[str, int]:
    """Mask personal data *inside* prose (comments, messages); return ``(text, hits)``.

    The outbound :func:`~schemaguru.pii.sweep` (shapes: email, IBAN, card, ...) plus the
    embedded person-name masking ``mask_value`` uses (``name_spans`` when names are enabled).
    Only for free text: identifiers (table/column names) never go through name masking.
    """
    masked, hits = sweep(text)
    if names_enabled():
        spans = name_spans(masked)
        if spans:
            out: list[str] = []
            pos = 0
            for a, b in spans:
                out.append(masked[pos:a])
                out.append("<name>")
                pos = b
            out.append(masked[pos:])
            masked, hits = "".join(out), hits + len(spans)
    return masked, hits


def _sweep_tree(node: Any) -> tuple[Any, int]:
    if isinstance(node, str):
        return sweep(node)
    if isinstance(node, list):
        out, hits = [], 0
        for v in node:
            s, h = _sweep_tree(v)
            out.append(s)
            hits += h
        return out, hits
    if isinstance(node, dict):
        d: dict[str, Any] = {}
        hits = 0
        for k, v in node.items():
            sk, hk = sweep(k)
            sv, hv = _sweep_tree(v)
            d[sk] = sv
            hits += hk + hv
        return d, hits
    return node, 0


def _rate(f: Fact | None, key: str) -> Any:
    return None if f is None else f.value.get(key)


def build_inputs(
    store: Store,
    table: ObjectRef,
    cfg: Config,
    *,
    examples: Mapping[str, Sequence[object]] | None = None,
) -> DraftInput:
    """Assemble the payload for ``table`` (must be in the store) under its value mode."""
    table = table.table_ref()
    t = store.table(table)
    if t is None:
        raise ValueError(f"table not in store: {table.canonical()}")
    mode = ValuePolicy.for_table(table, cfg.sampling)
    vals = _Values(mode)
    cols = store.columns(table)
    col_refs = [c.ref for c in cols]
    facts = store.facts_for([table, *col_refs], _STAT_KINDS)
    by_subject: dict[tuple[str, str], list[Fact]] = {}
    for f in facts:
        if isinstance(f.subject, ObjectRef):
            by_subject.setdefault((f.kind.name, f.subject.canonical()), []).append(f)

    n_examples = cfg.sampling.sample_values_per_column
    columns: list[dict[str, Any]] = []
    for c in sorted(cols, key=lambda c: c.ordinal):
        name = c.ref.column or ""
        key = c.ref.canonical()
        entry: dict[str, Any] = {
            "name": name,
            "type": c.native_type,
            "family": c.family,
            "nullable": c.nullable,
            "comment": c.comment,
            "null_rate": _rate(_newest(by_subject.get(("NULL_RATE", key), [])), "rate"),
            "distinct": _rate(_newest(by_subject.get(("DISTINCT", key), [])), "distinct_keys"),
        }
        values = by_subject.get(("VALUES", key), [])
        observed = _newest(values, Provenance.MEASURED)
        declared = _newest(values, Provenance.HUMAN) or _newest(values, Provenance.DECLARED)
        if mode != "none":
            obs = None if observed is None else observed.value.get("observed")
            if obs:
                entry["values"] = [
                    {"value": vals.one(name, o.get("value")), "count": o.get("count")} for o in obs
                ]
            if declared is not None and isinstance(declared.value.get("values"), list):
                entry["declared_values"] = [vals.one(name, v) for v in declared.value["values"]]
            ex = (examples or {}).get(name)
            if ex:
                entry["examples"] = [vals.one(name, v) for v in list(ex)[:n_examples]]
        columns.append(entry)

    cons = store.constraints(table)
    pk = next((list(k.columns) for k in cons if k.kind == "primary_key"), None)
    fks = [
        {
            "columns": list(k.columns),
            "to_table": k.ref_table.canonical() if k.ref_table is not None else None,
            "to_columns": list(k.ref_columns or ()),
        }
        for k in cons
        if k.kind == "foreign_key"
    ]
    neighbours = {fk["to_table"] for fk in fks if fk["to_table"]}
    for jf in store.facts_for(None, ["JOIN"]):
        j = jf.subject
        if isinstance(j, JoinRef):
            lt, rt = j.left[0].table_ref(), j.right[0].table_ref()
            if lt == table and rt != table:
                neighbours.add(rt.canonical())
            elif rt == table and lt != table:
                neighbours.add(lt.canonical())
    row_count = _rate(_newest(by_subject.get(("ROW_COUNT", table.canonical()), [])), "rows")
    payload: dict[str, Any] = {
        "table": table.canonical(),
        "kind": t.kind,
        "comment": t.comment,
        "row_count": row_count if row_count is not None else t.row_count,
        "columns": columns,
        "primary_key": pk,
        "foreign_keys": fks,
        "neighbours": sorted(neighbours),
    }
    hits = 0
    if mode != "raw":
        # free-text metadata first (shapes + names), then the shape sweep over everything
        for holder in (payload, *columns):
            if isinstance(holder.get("comment"), str):
                holder["comment"], h = mask_free_text(holder["comment"])
                hits += h
        payload, swept = _sweep_tree(payload)
        hits += swept
    return DraftInput(table, mode, payload, hits, tuple(sorted(vals.masked)))
