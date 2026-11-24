"""Run one agent tool (spec 18.1): JSON with a ``text`` field, never raises.

* **Errors are results.** A refused call (unknown tool, bad arguments, unknown or ambiguous
  name, a tool not offered) is ``{"error": {"code": "SG803", "message": ...}, "text": ...}``;
  an ambiguous bare name adds ``candidates`` (never a guess). Anything unexpected is
  ``SG804`` with the exception's type name and a masked message. A refused claim check is
  ``SG301``/``SG302`` (the measurement budget).
* **Tool results are outbound LLM payloads (spec 11).** Every value goes through the
  renderer's value policy (:class:`~schemaguru.render.table_block.ValuePresenter`: a PII column
  shows a whole shape token or ``<masked>``; ``none`` shows counts only); every free-text field
  through ``pii.mask_free_text``; and the whole result, text and structured fields alike, ends
  with :func:`~schemaguru.render.table_block.outbound` under the most restrictive mode of the
  tables it touches (``raw`` only when every one of them is opted in).
* **check_fact spends** on the warehouse: it is offered only with
  ``Config.agent_checks="allow"`` (default "deny", spec 8.2), admitted through the Guru's one
  per-period :class:`~schemaguru.measure.budget.BudgetGuard` (shared with the background
  drainer: one byte total; ``max_probes``/``max_seconds`` count across calls) and ledgered
  under ``agent_check``. Its evidence leaves as an allow-list (verdict, claim text with quoted
  literals as ``?``, SQL template, bytes, tier, duration): raw, sample and params never do.
* **Error messages** show every quoted literal as ``?`` unless values are raw (then echoes
  are cut to 200 characters), and are masked and swept.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any, cast

from schemaguru._util.hashing import canonical_json
from schemaguru.agent.tools import ToolHost, check_spec, tool_definitions
from schemaguru.errors import AmbiguousNameError, SchemaGuruError, StoreUnavailableError
from schemaguru.measure import flbridge
from schemaguru.model.detail import DetailLevel
from schemaguru.model.evidence import Evidence
from schemaguru.model.facts import Fact, Status
from schemaguru.model.refs import ObjectRef
from schemaguru.overlays.schema import GLOBAL_SUBJECT
from schemaguru.pii import ValuePolicy, mask_free_text
from schemaguru.pii.policy import Mode
from schemaguru.render.table_block import (
    ValuePresenter,
    ident,
    outbound,
    predicate_text,
    render_path,
)
from schemaguru.render.view import ViewRenderer

CHECK_LEDGER_KIND = "agent_check"
_RESTRICTION = {"none": 0, "masked": 1, "raw": 2}  # lower is more restrictive
_NOT_SWEPT = frozenset({"fingerprint"})  # a hash, not data; a hex sweep would mangle it
_JSON_TYPES: dict[str, Callable[[Any], bool]] = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, int | float) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "object": lambda v: isinstance(v, Mapping),
    "array": lambda v: isinstance(v, list),
}
_ARTICLE = {"integer": "an integer", "object": "an object", "array": "an array"}
MAX_ECHO = 200  # characters of a caller's value an error message may quote
# the outbound evidence fields (an allow-list: raw, sample and params never reach an agent)
_EVIDENCE_FIELDS = ("verdict", "claim_text", "sql", "bytes_processed", "tier", "duration_ms")


class ToolError(Exception):
    """A refused tool call; becomes an error result, never escapes ``handle_tool_call``."""

    def __init__(self, code: str, message: str, candidates: Iterable[str] = ()) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.candidates = list(candidates)


# ----- result helpers --------------------------------------------------------------------------


def _error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, **extra, "message": message}
    return {"error": err, "text": f"{code} {message}"}


_LONG_QUOTE = re.compile(
    r"'([^'\n]{" + str(MAX_ECHO) + r"})[^'\n]+'" + r'|"([^"\n]{' + str(MAX_ECHO) + r'})[^"\n]+"'
)
# a quoted literal in a message: '...' (SQL doubling or backslash escapes) or "..." (a repr)
_QUOTED = re.compile(r"'(?:[^'\\\n]|\\.|'')*'|\"(?:[^\"\\\n]|\\.)*\"")


def _cut(m: re.Match[str]) -> str:
    if m.group(1) is not None:
        return f"'{m.group(1)}…'"
    return f'"{m.group(2)}…"'


def error_text(guru: ToolHost, text: str) -> str:
    """An error message as it leaves (spec 11): unless values are raw, every quoted literal
    (a caller's value, a value quoted by a backend) becomes ``?``; raw keeps them, cut to
    ``MAX_ECHO`` characters. Then free-text masking and the outbound sweep. The tools' own
    argument names are written in backticks, so they stay readable."""
    try:
        mode = outbound_mode(guru)
    except Exception:
        mode = "none"  # cannot tell: the most restrictive
    if mode == "raw":
        return _LONG_QUOTE.sub(_cut, text)
    text = _QUOTED.sub("?", text)
    text = mask_free_text(text)[0]
    return outbound(text, mode)[0]


def _strictest(modes: Iterable[str]) -> str:
    return min(modes, key=lambda m: _RESTRICTION.get(m, 0))


def outbound_mode(guru: ToolHost, tables: Iterable[ObjectRef] = ()) -> str:
    """The most restrictive value mode over ``tables``; with none, over every configured mode
    (a result tied to no table must not widen any glob)."""
    sampling = guru.config.sampling
    modes: list[str] = [ValuePolicy.for_table(t, sampling) for t in tables]
    if not modes:
        modes = [sampling.llm_values, *sampling.per_glob.values()]
    return _strictest(modes)


def _outbound_tree(node: Any, mode: str, key: str | None = None) -> Any:
    if isinstance(node, str):
        return node if key in _NOT_SWEPT else outbound(node, mode)[0]
    if isinstance(node, Mapping):
        return {str(k): _outbound_tree(v, mode, str(k)) for k, v in node.items()}
    if isinstance(node, list | tuple):
        return [_outbound_tree(v, mode, key) for v in node]
    return node


def _result(result: Mapping[str, Any], mode: str) -> dict[str, Any]:
    """A tool result as it leaves: JSON-safe, every string through the outbound sweep."""
    out: dict[str, Any] = _outbound_tree(_json_safe(result), mode)
    return out


def _json_safe(obj: Any) -> Any:
    return json.loads(canonical_json(obj))


def _free(text: str, mode: str) -> str:
    return text if mode == "raw" else mask_free_text(text)[0]


# ----- arguments -------------------------------------------------------------------------------


def _validate(spec: Mapping[str, Any], args: Mapping[str, Any]) -> dict[str, Any]:
    """Check ``args`` against the tool's input schema (the subset the tools use)."""
    schema = spec["input_schema"]
    props: Mapping[str, Any] = schema.get("properties", {})
    for name in schema.get("required", []):
        if name not in args:
            raise ToolError("SG803", f"{spec['name']}: missing required argument `{name}`")
    out: dict[str, Any] = {}
    for name, value in args.items():
        prop = props.get(name)
        if prop is None:
            known = ", ".join(props) or "none"
            raise ToolError(
                "SG803", f"{spec['name']}: unexpected argument {name!r} (arguments: {known})"
            )
        typ = prop.get("type")
        check = _JSON_TYPES.get(typ) if isinstance(typ, str) else None
        if check is not None and not check(value):
            article = _ARTICLE.get(str(typ), f"a {typ}")
            raise ToolError("SG803", f"{spec['name']}: `{name}` must be {article}")
        if isinstance(value, int | float) and not isinstance(value, bool):
            lo, hi = prop.get("minimum"), prop.get("maximum")
            if lo is not None and value < lo:
                raise ToolError("SG803", f"{spec['name']}: `{name}` must be >= {lo}")
            if hi is not None and value > hi:
                raise ToolError("SG803", f"{spec['name']}: `{name}` must be <= {hi}")
        if isinstance(value, str) and len(value.strip()) < int(prop.get("minLength", 0)):
            raise ToolError("SG803", f"{spec['name']}: `{name}` must not be empty")
        cap = prop.get("maxLength")
        if isinstance(value, str) and isinstance(cap, int) and len(value) > cap:
            raise ToolError("SG803", f"{spec['name']}: `{name}` is longer than {cap} characters")
        out[name] = value
    return out


def _table(guru: ToolHost, name: str) -> ObjectRef:
    try:
        return guru.table_ref(name)
    except AmbiguousNameError as e:
        raise ToolError("SG803", str(e), e.candidates) from None


def _column(guru: ToolHost, name: str) -> ObjectRef:
    try:
        return guru.column_ref(name)
    except AmbiguousNameError as e:
        raise ToolError("SG803", str(e), e.candidates) from None


# ----- the tools -------------------------------------------------------------------------------


def _schema_context(guru: ToolHost, args: dict[str, Any]) -> dict[str, Any]:
    view = guru.context(args["question"], budget_tokens=int(args.get("budget_tokens", 4000)))
    result = {
        "text": view.text,
        "tables": [t.canonical() for t in view.tables],
        "fingerprint": view.fingerprint,
    }
    return _result(result, outbound_mode(guru, view.tables))


def _describe(r: ViewRenderer, ref: ObjectRef) -> str | None:
    """A table's description through its value policy (free text masked)."""
    return ValuePresenter(r.knowledge(ref)).run().description


def _search_tables(guru: ToolHost, args: dict[str, Any]) -> dict[str, Any]:
    k = int(args.get("k", 10))
    scores: dict[ObjectRef, float] = {}
    for ref, score in guru.retriever.search(args["query"], k):
        scores.setdefault(ref.table_ref(), score)
    refs = list(scores)[:k]
    descriptions = guru._with_renderer(lambda r: [_describe(r, ref) for ref in refs])
    pairs = [(ref.canonical(), d or "") for ref, d in zip(refs, descriptions, strict=True)]
    rows = [
        {"table": name, "score": scores[ref], "description": d}
        for (name, d), ref in zip(pairs, refs, strict=True)
    ]
    text = "\n".join(name + (f" — {d}" if d else "") for name, d in pairs)
    result = {"text": text or "no matching tables", "tables": rows}
    return _result(result, outbound_mode(guru, refs))


def _describe_table(guru: ToolHost, args: dict[str, Any]) -> dict[str, Any]:
    ref = _table(guru, args["table"])
    text = guru._with_renderer(lambda r: r.block_text(ref, DetailLevel.FULL))
    return _result({"text": text, "table": ref.canonical()}, outbound_mode(guru, [ref]))


def _find_join_path(guru: ToolHost, args: dict[str, Any]) -> dict[str, Any]:
    src, dst = _table(guru, args["from"]), _table(guru, args["to"])
    res = guru.find_path(src, dst)
    lines: list[str] = []
    for p in res.paths:
        lines += render_path(p)
    if res.refusal is not None or res.explanation:
        lines.append(res.explanation)
    paths = [
        {
            "tables": [t.canonical() for t in p.tables],
            "coverage": p.coverage,
            "provisional": p.provisional,
            "cost": p.cost,
        }
        for p in res.paths
    ]
    result = {
        "text": "\n".join(lines),
        "refusal": None if res.refusal is None else res.refusal.value,
        "paths": paths,
    }
    tables = [src, dst, *(t for p in res.paths for t in p.tables)]
    return _result(result, outbound_mode(guru, tables))


_VALUE_SCALARS = ("distinct", "non_null", "case_sensitive", "split", "fraction", "approximate")


def _value_entry(p: ValuePresenter, column: str, f: Fact) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "provenance": f.provenance.value,
        "status": f.status.value,
        "method": f.method,
    }
    if f.status in (Status.ERROR, Status.UNMEASURABLE):
        entry["reason"] = p.reason(f)
    for key in _VALUE_SCALARS:
        v = f.value.get(key)
        if (v is None or isinstance(v, bool | int | float | str)) and key in f.value:
            entry[key] = v
    declared = f.value.get("values")
    observed = f.value.get("observed")
    hidden = p.mode == "none"
    if isinstance(declared, list):
        if hidden:
            entry["declared_count"] = len(declared)
        else:
            entry["values"] = [p.shown(column, v) for v in declared]
    if isinstance(observed, list):
        items = [i for i in observed if isinstance(i, Mapping)]
        if hidden:
            entry["observed_count"] = len(items)
        else:
            entry["observed"] = [
                {
                    "value": p.shown(column, i.get("value")),
                    "count": i.get("count") if isinstance(i.get("count"), int) else None,
                }
                for i in items
            ]
    return entry


def _column_values(guru: ToolHost, args: dict[str, Any]) -> dict[str, Any]:
    col = _column(guru, args["column"])
    name = col.column
    assert name is not None

    def build(r: ViewRenderer) -> dict[str, Any]:
        k = r.knowledge(col.table_ref())
        p = ValuePresenter(k)
        a = p.run()
        eff = k.get("VALUES", name)
        entries = [] if eff is None else [_value_entry(p, name, f) for f in eff.all]
        lines = [col.canonical()]
        for view in a.columns:
            if view.column.ref.column == name:
                lines += [f"  {t}" for _, t in view.notes if t.startswith("values")]
        q = ident(name) + ":"
        lines += [f"  ⚠ {w}" for w in a.warnings if q in w]
        if not entries:
            lines.append("  no values known")
        return {
            "text": "\n".join(lines),
            "column": col.canonical(),
            "mode": p.mode,
            "entries": entries,
        }

    result = guru._with_renderer(build)
    return _result(result, str(result["mode"]))


def _maps_to(raw: Any) -> list[ObjectRef]:
    out: list[ObjectRef] = []
    for m in raw if isinstance(raw, list) else []:
        try:
            if isinstance(m, Mapping):
                out.append(ObjectRef.from_dict(dict(m)).table_ref())
            elif isinstance(m, str):
                out.append(ObjectRef.parse(m).table_ref())
        except (KeyError, ValueError):
            continue
    return out


def _explain_term(guru: ToolHost, args: dict[str, Any]) -> dict[str, Any]:
    term = args["term"].strip().casefold()
    found: list[tuple[Fact, str, list[ObjectRef]]] = []
    for f in guru.store.facts_for([GLOBAL_SUBJECT], ["GLOSSARY", "METRIC"]):
        name = f.value.get("term") or f.value.get("name")
        if isinstance(name, str) and name.strip().casefold() == term:
            found.append((f, name, _maps_to(f.value.get("maps_to"))))
    tables = [t for _, _, ts in found for t in ts]
    mode = _strictest([outbound_mode(guru, tables), guru.config.sampling.llm_values])
    entries: list[dict[str, Any]] = []
    for f, name, maps in found:
        definition = f.value.get("definition")
        entries.append(
            {
                "term": _free(name, mode),
                "kind": f.kind.name,
                "definition": None if not isinstance(definition, str) else _free(definition, mode),
                "maps_to": [t.canonical() for t in maps],
                "provenance": f.provenance.value,
            }
        )
    text = "\n".join(
        f"{e['term']}: {e['definition']} ({e['provenance']})"
        + (f" — tables: {', '.join(e['maps_to'])}" if e["maps_to"] else "")
        for e in entries
    )
    empty = f"no glossary entry for {_free(args['term'], mode)!r}"
    return _result({"text": text or empty, "entries": entries}, mode)


def _check_fact(guru: ToolHost, args: dict[str, Any], spec: Mapping[str, Any]) -> dict[str, Any]:
    schema = spec["input_schema"]
    names = [n for n in schema.get("required", []) if isinstance(args.get(n), str)]
    if not names:
        raise ToolError("SG803", "check_fact: no claim text given")
    claims = str(args[names[0]])
    # the measurement period guard: one byte total, and max_probes / max_seconds count across
    # calls (no run is started here; a drainer run restarts the probe count and the clock)
    guard = guru._agent_guard()
    key = object()  # one admission per call
    estimator = getattr(guru.backend, "estimate_claims", None)
    estimate = estimator(claims) if callable(estimator) else None
    known = estimate if isinstance(estimate, int) and not isinstance(estimate, bool) else None
    verdict = guard.admit_key(key, known)
    if verdict == "over_probe":
        raise ToolError("SG301", "check_fact: the claim check exceeds the per-probe byte budget")
    if verdict == "over_total":
        raise ToolError("SG302", "check_fact: the measurement budget is exhausted; not run")
    note = f"check_fact ({claims.count(chr(10)) + 1} line(s))"
    try:
        evidence = list(guru.backend.check(claims))
    except BaseException:
        guard.record_key(key, None, note, kind=CHECK_LEDGER_KIND)
        raise
    sizes = [e.bytes_processed for e in evidence]
    reported = sum(s for s in sizes if s is not None) if sizes and None not in sizes else None
    guard.record_key(key, reported, note, kind=CHECK_LEDGER_KIND)
    mode = outbound_mode(guru)  # a claim may name any table: every configured mode counts
    scrubbed = [_outbound_evidence(e, mode) for e in evidence]
    text = "\n".join(f"{e.get('verdict')}: {e.get('claim_text')}" for e in scrubbed)
    result = {"text": text or "no verdict (the backend cannot check claims)", "evidence": scrubbed}
    return _result(result, mode)


def _outbound_evidence(ev: Evidence, mode: str) -> dict[str, Any]:
    """Evidence an agent may see: the allow-listed fields only. The claim's quoted literals
    become ``?`` unless values are raw (a quoted value may belong to a PII column, and no
    column policy can be applied to claim text); the SQL is its template."""
    full = _json_safe(flbridge.scrub_evidence(ev, cast(Mode, mode)).to_dict())
    out = {k: full.get(k) for k in _EVIDENCE_FIELDS}
    claim = out.get("claim_text")
    if isinstance(claim, str) and mode != "raw":
        out["claim_text"] = predicate_text(claim, "none")[0]
    return out


_HANDLERS: dict[str, Callable[[ToolHost, dict[str, Any]], dict[str, Any]]] = {
    "schema_context": _schema_context,
    "search_tables": _search_tables,
    "describe_table": _describe_table,
    "find_join_path": _find_join_path,
    "column_values": _column_values,
    "explain_term": _explain_term,
}


def _run(guru: ToolHost, name: Any, args: Any) -> dict[str, Any]:
    if not isinstance(name, str):
        raise ToolError("SG803", f"the tool name must be a string, not {type(name).__name__}")
    if name == "check_fact":
        if guru.config.agent_checks != "allow":
            raise ToolError("SG803", "check_fact is disabled (Config.agent_checks='deny')")
        spec = check_spec(guru)
        if spec is None:
            raise ToolError(
                "SG803", "check_fact is not available: the installed FactLang has no agent tools"
            )
    else:
        known = {s["name"]: s for s in tool_definitions(guru)}
        if name not in known:
            listed = ", ".join(known)
            raise ToolError("SG803", f"unknown tool {name!r}; known: {listed}")
        spec = known[name]
    if not isinstance(args, Mapping):
        raise ToolError("SG803", f"{name}: arguments must be an object")
    clean = _validate(spec, args)
    if name == "check_fact":
        return _check_fact(guru, clean, spec)
    return _HANDLERS[name](guru, clean)


def handle_tool_call(guru: ToolHost, name: str, args: Mapping[str, Any]) -> dict[str, Any]:
    """Run tool ``name`` with ``args``; always a JSON-safe dict with ``text``; never raises."""
    try:
        return _run(guru, name, args)
    except ToolError as e:
        extra: dict[str, Any] = {"candidates": e.candidates} if e.candidates else {}
        return _error(e.code, error_text(guru, e.message), **extra)
    except StoreUnavailableError as e:  # a shared store is down: not the caller's fault
        return _error("SG703", error_text(guru, str(e)))
    except SchemaGuruError as e:
        return _error("SG803", error_text(guru, f"{name}: {e}"))
    except Exception as e:  # a tool result is always JSON (spec 18.1)
        return _error("SG804", error_text(guru, str(e)), type=type(e).__name__)


__all__ = ["CHECK_LEDGER_KIND", "ToolError", "error_text", "handle_tool_call", "outbound_mode"]
