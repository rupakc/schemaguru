"""The drafting run (spec 10.2, 10.4): tables in, ``DRAFTED`` facts out.

* One provider call per table; tables with at most ``SMALL_TABLE_COLUMNS`` columns are batched,
  up to ``BATCH_SIZE`` per call.
* A reply entry that fails the schema or names a column/table that does not exist is retried
  once (only the failed tables); a second failure records that table's draft as one ``ERROR``
  fact (``SG401``) and applies nothing of it. A provider exception is ``SG403`` (the SDKs retry
  transport errors themselves).
* Drafts are ``DRAFTED`` / ``UNVERIFIED`` facts with ``source=f"llm:{provider.name}:{model}"``
  and ``value["prompt_hash"]``. They never replace a ``HUMAN`` fact: provenance is part of a
  fact's id, so both are kept and precedence (``knowledge.resolve``) picks ``HUMAN``.
* Token usage per call goes to the store ledger (kind ``draft``, unit ``tokens``); the run stops
  before a call that would exceed ``draft_budget_tokens`` (``SG402``, the rest is ``skipped``).
  A provider that reports no usage is charged an estimate of ~4 characters per token.

Drafting is an explicit run: it is never called from scoping, rendering or the query path.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from importlib import resources
from typing import Any

from schemaguru._util.hashing import canonical_json, stable_hash
from schemaguru._util.timeutil import require_aware, utcnow
from schemaguru.config import Budget, Config
from schemaguru.diagnostics import Diagnostic, Severity
from schemaguru.draft.inputs import DraftInput, build_inputs
from schemaguru.draft.provider import (
    DRAFT_SCHEMA,
    TABLE_DRAFT_SCHEMA,
    InvalidReply,
    LLMProvider,
    Usage,
    validate,
)
from schemaguru.model.facts import Fact, Provenance, Status
from schemaguru.model.joins import JOIN_VALUE_KEYS, JoinRef
from schemaguru.model.kinds import FactKind, KindSpec
from schemaguru.model.refs import ObjectRef
from schemaguru.overlays.schema import GLOBAL_SUBJECT
from schemaguru.store.protocol import Store

PROMPT_NAME = "table_v1.md"
SMALL_TABLE_COLUMNS = 5
BATCH_SIZE = 8
CHARS_PER_TOKEN = 4
LEDGER_KIND = "draft"


@lru_cache(maxsize=8)
def load_prompt(name: str = PROMPT_NAME) -> str:
    return resources.files("schemaguru.draft").joinpath("prompts").joinpath(name).read_text("utf-8")


def prompt_hash(name: str = PROMPT_NAME) -> str:
    """Hash of the prompt text and the output schema: recorded on every drafted fact."""
    return stable_hash({"prompt": load_prompt(name), "schema": DRAFT_SCHEMA})


@dataclass
class DraftReport:
    drafted: list[Fact] = field(default_factory=list)
    errors: list[Fact] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    skipped: list[ObjectRef] = field(default_factory=list)
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_tokens: int = 0  # charged for calls whose provider reported no usage
    pii_sweep_hits: int = 0

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.estimated_tokens


def _diag(code: str, message: str, location: str | None = None) -> Diagnostic:
    sev = Severity.WARNING if code == "SG402" else Severity.ERROR
    return Diagnostic(code, sev, message, location)


class _Failure(Exception):
    def __init__(self, code: str, reason: str, message: str) -> None:
        super().__init__(message)
        self.code, self.reason, self.message = code, reason, message


def _cap(budget: Budget | int | None, cfg: Config) -> int | None:
    if isinstance(budget, int):
        return budget
    return (budget or cfg.budget).draft_budget_tokens


def _groups(inputs: list[tuple[DraftInput, int]]) -> list[list[DraftInput]]:
    groups: list[list[DraftInput]] = []
    batch: list[DraftInput] | None = None
    for inp, ncols in inputs:
        if ncols > SMALL_TABLE_COLUMNS:
            groups.append([inp])
            continue
        if batch is None or len(batch) >= BATCH_SIZE:
            batch = []
            groups.append(batch)
        batch.append(inp)
    return groups


class _Run:
    def __init__(
        self,
        store: Store,
        provider: LLMProvider,
        cfg: Config,
        cap: int | None,
        max_tokens: int,
        now: datetime,
    ) -> None:
        self.store, self.provider, self.cfg, self.cap = store, provider, cfg, cap
        self.max_tokens, self.now = max_tokens, now
        model = getattr(provider, "model", None) or cfg.llm_model
        self.source = f"llm:{provider.name}:{model}"
        self.system = load_prompt()
        self.phash = prompt_hash()
        self.report = DraftReport()
        self.stopped = False

    # --- accounting ----------------------------------------------------------------
    def _charge(self, user: str, reply: Mapping[str, Any] | None, n: int) -> None:
        usage = getattr(self.provider, "last_usage", None)
        r = self.report
        if isinstance(usage, Usage):
            r.input_tokens += usage.input_tokens
            r.output_tokens += usage.output_tokens
            amount, note = usage.input_tokens + usage.output_tokens, "reported"
        else:
            chars = len(self.system) + len(user) + len(canonical_json(reply or {}))
            amount, note = -(-chars // CHARS_PER_TOKEN), "estimated"
            r.estimated_tokens += amount
        self.store.ledger_add(LEDGER_KIND, amount, "tokens", f"{self.source} {n} table(s) {note}")

    def _over_budget(self, user: str) -> bool:
        if self.cap is None:
            return False
        est_in = -(-(len(self.system) + len(user)) // CHARS_PER_TOKEN)
        return self.report.tokens >= self.cap or self.report.tokens + est_in > self.cap

    # --- one group -----------------------------------------------------------------
    def run_group(self, group: list[DraftInput]) -> None:
        pending = list(group)
        failures: dict[str, _Failure] = {}
        for _attempt in range(2):
            user = canonical_json({"tables": [i.payload for i in pending]})
            if self._over_budget(user):
                self.stopped = True
                self.report.skipped += [i.table for i in pending]
                return
            self.report.calls += 1
            reply: dict[str, Any] | None = None
            try:
                reply = self.provider.complete_json(
                    self.system, user, DRAFT_SCHEMA, max_tokens=self.max_tokens
                )
                outcome = self._parse(reply, pending)
            except InvalidReply as e:
                outcome = {
                    i.table.canonical(): _Failure("SG401", "invalid_reply", str(e)) for i in pending
                }
            except Exception as e:  # a provider failure costs this group, never the run
                self._charge(user, None, len(pending))
                for i in pending:
                    self._error(i, _Failure("SG403", "provider_error", f"{type(e).__name__}: {e}"))
                return
            self._charge(user, reply, len(pending))
            failed: list[DraftInput] = []
            for i in pending:
                res = outcome[i.table.canonical()]
                if isinstance(res, _Failure):
                    failures[i.table.canonical()] = res
                    failed.append(i)
                else:
                    self.store.put_facts(res)
                    self.report.drafted += res
            pending = failed
            if not pending:
                return
        for i in pending:
            self._error(i, failures[i.table.canonical()])

    def _error(self, inp: DraftInput, f: _Failure) -> None:
        fact = Fact.make(
            FactKind.DESCRIPTION,
            inp.table,
            {
                "text": None,
                "reason": f.reason,
                "code": f.code,
                "message": f.message[:500],
                "prompt_hash": self.phash,
            },
            Provenance.DRAFTED,
            Status.ERROR,
            source=self.source,
            observed_at=self.now,
        )
        self.store.put_facts([fact])
        self.report.errors.append(fact)
        self.report.diagnostics.append(
            _diag(f.code, f"draft failed: {f.message}", inp.table.canonical())
        )

    def _parse(self, reply: Any, pending: list[DraftInput]) -> dict[str, list[Fact] | _Failure]:
        if not isinstance(reply, dict) or not isinstance(reply.get("tables"), list):
            raise InvalidReply("reply has no 'tables' list")
        extra = [k for k in reply if k != "tables"]
        if extra:
            raise InvalidReply(f"reply has unexpected keys {extra}")
        entries: dict[str, Any] = {}
        for e in reply["tables"]:
            name = e.get("table") if isinstance(e, dict) else None
            if isinstance(name, str) and name not in entries:
                entries[name] = e
        out: dict[str, list[Fact] | _Failure] = {}
        for inp in pending:
            key = inp.table.canonical()
            entry = entries.get(key)
            if entry is None:
                out[key] = _Failure("SG401", "invalid_reply", f"no draft for {key} in the reply")
                continue
            errs = validate(entry, TABLE_DRAFT_SCHEMA, f"$.tables[{key}]")
            if not errs:
                try:
                    out[key] = self._facts(entry, inp)
                    continue
                except ValueError as e:
                    errs = [str(e)]
            out[key] = _Failure("SG401", "invalid_reply", "; ".join(errs[:5]))
        return out

    # --- reply -> facts ------------------------------------------------------------
    def _facts(self, e: Mapping[str, Any], inp: DraftInput) -> list[Fact]:
        t = inp.table
        names = {c.ref.column for c in self.store.columns(t)}

        def col(name: str, where: str) -> ObjectRef:
            if name not in names:
                raise ValueError(f"{where}: {t.canonical()} has no column {name!r}")
            return ObjectRef(t.catalog, t.schema, t.table, name)

        out: list[Fact] = []

        def add(kind: KindSpec, subject: ObjectRef | JoinRef, value: dict[str, Any]) -> None:
            out.append(
                Fact.make(
                    kind,
                    subject,
                    {**value, "prompt_hash": self.phash},
                    Provenance.DRAFTED,
                    Status.UNVERIFIED,
                    source=self.source,
                    observed_at=self.now,
                )
            )

        add(FactKind.DESCRIPTION, t, {"text": e["description"]})
        for c in e["columns"]:
            add(FactKind.DESCRIPTION, col(c["name"], "columns"), {"text": c["description"]})
        if e["use_cases"]:
            add(FactKind.USE_CASES, t, {"questions": list(e["use_cases"])})
        ent = e["entity_type"]
        if ent is not None:
            for k in ent["key"]:
                col(k, "entity_type.key")
            add(FactKind.ENTITY_TYPE, t, {"entity": ent["entity"], "key": list(ent["key"])})
        for p in e["pitfalls"]:
            add(
                FactKind.PITFALL, t, {"rule": p["rule"], "fix": p["fix"], "severity": p["severity"]}
            )
        sd = e["soft_delete"]
        if sd is not None:
            for k in sd["marker_columns"]:
                col(k, "soft_delete.marker_columns")
            add(
                FactKind.SOFT_DELETE,
                t,
                {"predicate": sd["predicate"], "marker_columns": list(sd["marker_columns"])},
            )
        for n in e["type_notes"]:
            pat = n["pattern"]
            if pat is not None and not pat.strip("%_"):
                raise ValueError(f"type_notes: pattern {pat!r} would match every value")
            add(
                FactKind.TYPE_NOTE,
                col(n["column"], "type_notes"),
                {"stored_as": n["stored_as"], "means": n["means"], "pattern": pat},
            )
        for p in e["pii"]:
            add(
                FactKind.PII,
                col(p["column"], "pii"),
                {"category": p["category"], "detector": "llm"},
            )
        for j in e["join_candidates"]:
            add(
                FactKind.JOIN,
                self._join(j, col),
                {**{k: None for k in JOIN_VALUE_KEYS}, "candidate": True},
            )
        for g in e["glossary"]:
            add(
                FactKind.GLOSSARY,
                GLOBAL_SUBJECT,
                {"term": g["term"], "definition": g["definition"], "maps_to": [t.to_dict()]},
            )
        return out

    def _join(self, j: Mapping[str, Any], col: Any) -> JoinRef:
        left = tuple(col(c, "join_candidates.columns") for c in j["columns"])
        target, cands = self.store.resolve(j["to_table"])
        if target is None or target.column is not None or self.store.table(target) is None:
            more = f" (ambiguous: {[c.canonical() for c in cands]})" if cands else ""
            raise ValueError(f"join_candidates: unknown table {j['to_table']!r}{more}")
        tnames = {c.ref.column for c in self.store.columns(target)}
        bad = [c for c in j["to_columns"] if c not in tnames]
        if bad or len(j["to_columns"]) != len(left) or not left:
            raise ValueError(f"join_candidates: bad target columns {j['to_columns']!r}")
        right = tuple(
            ObjectRef(target.catalog, target.schema, target.table, c) for c in j["to_columns"]
        )
        return JoinRef(left, right)


def draft_tables(
    refs: Iterable[ObjectRef],
    *,
    store: Store,
    provider: LLMProvider,
    config: Config | None = None,
    budget: Budget | int | None = None,
    max_tokens: int = 8192,
    now: datetime | None = None,
) -> DraftReport:
    """Draft the meaning of ``refs`` and store the drafts; see the module docstring."""
    cfg = config or Config()
    run = _Run(
        store,
        provider,
        cfg,
        _cap(budget, cfg),
        max_tokens,
        utcnow() if now is None else require_aware(now),
    )
    report = run.report
    inputs: list[tuple[DraftInput, int]] = []
    for ref in sorted({r.table_ref() for r in refs}, key=lambda r: r.canonical()):
        if store.table(ref) is None:
            report.diagnostics.append(
                _diag("SG404", "table is not in the store (crawl it first)", ref.canonical())
            )
            continue
        inp = build_inputs(store, ref, cfg)
        report.pii_sweep_hits += inp.pii_sweep_hits
        inputs.append((inp, len(store.columns(ref))))
    for group in _groups(inputs):
        if run.stopped:
            report.skipped += [i.table for i in group]
            continue
        run.run_group(group)
    if report.skipped:
        report.diagnostics.append(
            _diag(
                "SG402",
                f"draft token budget ({run.cap}) exhausted; {len(report.skipped)} table(s) "
                "were not drafted",
            )
        )
    return report
