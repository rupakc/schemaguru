"""Context views (spec 13.1, 12.1 steps 4-6, 12.3).

A view is an ordered list of blocks: the *stable* ones first (global pitfalls, never-query list,
legend: the same for every question, so a host can place a prompt-cache breakpoint after them),
then the question's glossary entries, table blocks in rank order, join paths between the shown
tables, and a closing "Not shown" line.

:class:`ViewRenderer` is the render layer's ``scope.TableRenderer``: ``render(ref, level)``
returns a block *plus its separator*, so the budget the scope fitter spends includes the
separators between blocks; :meth:`ViewRenderer.table_budget` reserves the always-on blocks.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime

from schemaguru._util.hashing import stable_hash
from schemaguru._util.timeutil import require_aware
from schemaguru._util.tokens import CharTokenCounter, TokenCounter
from schemaguru.config import Config
from schemaguru.diagnostics import Diagnostic, DiagnosticBag, Severity
from schemaguru.graph.search import Automaton, Graph, Path, Refusal, find_paths
from schemaguru.knowledge.resolve import resolve
from schemaguru.model.detail import DetailLevel
from schemaguru.model.facts import Fact, Provenance, Status, Waiver
from schemaguru.model.refs import ObjectRef
from schemaguru.overlays.schema import GLOBAL_SUBJECT
from schemaguru.pii.mask import mask_free_text
from schemaguru.pii.policy import ValuePolicy
from schemaguru.render import formats
from schemaguru.render.fragments import render_join_snippet
from schemaguru.render.knowledge import TableKnowledge, collect
from schemaguru.render.legend import LEGEND, WARN, needs_legend
from schemaguru.render.profiles import Profile, get_profile
from schemaguru.render.table_block import (
    analyse,
    outbound,
    pitfall_text,
    predicate_text,
    qname,
    render_path,
    severity,
    tag,
)
from schemaguru.store.protocol import Store

SEPARATOR = "\n\n"
# The "Not shown" line is reserved at this count, so the real line always fits.
NOT_SHOWN_RESERVE = 999_999
_PATHS_HEADER = "Join paths (join keys, advisory; support = the weakest hop's match or coverage):"
_HAZARD_REFUSALS = frozenset(
    {Refusal.HUB_PIVOT, Refusal.RULE_REJECTS_ALL, Refusal.WAYPOINT_UNSATISFIABLE}
)
_PREFERENCE = (
    Provenance.HUMAN,
    Provenance.DECLARED,
    Provenance.CATALOG,
    Provenance.MEASURED,
    Provenance.DRAFTED,
)


@dataclass(frozen=True)
class ViewBlock:
    """One block of a view. ``stable`` blocks do not depend on the question."""

    stable: bool
    text: str
    kind: str = "table"
    title: str | None = None


@dataclass(frozen=True)
class ContextView:
    """What an agent reads for one question (spec 13.1)."""

    question: str
    blocks: tuple[ViewBlock, ...]
    tables: tuple[ObjectRef, ...]
    fingerprint: str
    tokens: int
    diagnostics: tuple[Diagnostic, ...] = field(default=())
    # what ``str(view)`` renders (a registered format name); not part of the view's identity
    format: str = field(default="text", compare=False)

    @property
    def text(self) -> str:
        return SEPARATOR.join(b.text for b in self.blocks)

    def __str__(self) -> str:
        return formats.format_view(self, self.format)

    @classmethod
    def build(
        cls,
        question: str,
        blocks: Sequence[ViewBlock],
        *,
        tables: Sequence[ObjectRef] = (),
        counter: TokenCounter | None = None,
        diagnostics: Sequence[Diagnostic] = (),
    ) -> ContextView:
        text = SEPARATOR.join(b.text for b in blocks)
        n = (counter or CharTokenCounter()).count(text)
        return cls(question, tuple(blocks), tuple(tables), stable_hash(text), n, tuple(diagnostics))

    def to_markdown(self) -> str:
        return formats.to_markdown(self)

    def to_json(self) -> str:
        return formats.to_json(self)

    def to_xml(self) -> str:
        return formats.to_xml(self)


def _not_shown(n: int) -> ViewBlock:
    if n <= 0:
        text = "Not shown: no other candidate tables."
    else:
        noun = "table" if n == 1 else "tables"
        text = f"Not shown: {n} other candidate {noun} (search_tables / describe_table)."
    return ViewBlock(False, text, "not_shown", "Not shown")


def _preferred(facts: Sequence[Fact]) -> list[Fact]:
    """The facts of the most authoritative provenance present (HUMAN beats DRAFTED)."""
    for prov in _PREFERENCE:
        chosen = [
            f for f in facts if f.provenance is prov and f.value and f.status is not Status.ERROR
        ]
        if chosen:
            return chosen
    return []


def _refs(raw: object) -> list[ObjectRef]:
    out: list[ObjectRef] = []
    for item in raw if isinstance(raw, list) else []:
        try:
            if isinstance(item, Mapping):
                out.append(ObjectRef.from_dict(dict(item)).table_ref())
            elif isinstance(item, str):
                out.append(ObjectRef.parse(item))
        except (KeyError, ValueError):
            continue  # one malformed entry costs that entry
    return out


class ViewRenderer:
    """Renders tables and whole views for one store, time, config and profile.

    Implements ``scope.TableRenderer``. Read-only; per-table knowledge is cached for the
    renderer's life, so create one per question (or per store snapshot).
    """

    def __init__(
        self,
        store: Store,
        *,
        now: datetime,
        cfg: Config,
        profile: str | Profile = "agent",
        graph: Graph | None = None,
        waivers: Sequence[Waiver] = (),
        dialect: str | None = None,
        rules: Callable[[ObjectRef, ObjectRef], Automaton | None] | None = None,
    ) -> None:
        """``rules(src, dst)`` gives the waypoint automaton for a join-path search, if any."""
        self.store = store
        self.now = require_aware(now)
        self.cfg = cfg
        self.profile = get_profile(profile) if isinstance(profile, str) else profile
        self.graph = graph
        self.waivers = tuple(waivers)
        self.dialect = dialect
        self.rules = rules
        self._knowledge: dict[ObjectRef, TableKnowledge] = {}
        self._joins: list[Fact] | None = None
        self._globals: list[ViewBlock] | None = None
        self._glossary: list[Fact] | None = None
        self._global_masked = 0
        self._gloss_masked = 0
        self._last_hits = 0
        self._scans: dict[tuple[ObjectRef, ...], tuple[dict[str, list[str]], list[Path]]] = {}

    # ----- per table ---------------------------------------------------------------------

    def knowledge(self, ref: ObjectRef) -> TableKnowledge:
        ref = ref.table_ref()
        k = self._knowledge.get(ref)
        if k is None:
            if self._joins is None:
                self._joins = self.store.facts_for(None, ["JOIN"])
            k = collect(
                self.store, ref, self.now, self.cfg, join_facts=self._joins, waivers=self.waivers
            )
            self._knowledge[ref] = k
        return k

    def block_text(self, ref: ObjectRef, level: DetailLevel) -> str:
        return self.profile.render_table(self.knowledge(ref), level)

    @property
    def compact(self) -> bool:
        """A compact profile (``compact = True``) packs its one-line tables into one block."""
        return bool(getattr(self.profile, "compact", False))

    def render(self, ref: ObjectRef, level: DetailLevel) -> str:
        """``scope.TableRenderer``: the block plus the separator that follows it."""
        return self.block_text(ref, level) + ("\n" if self.compact else SEPARATOR)

    # ----- always-on blocks --------------------------------------------------------------

    def global_blocks(self) -> list[ViewBlock]:
        """Global pitfalls and the never-query list (stable; spec 12.1 step 5)."""
        if self._globals is not None:
            return self._globals
        out: list[ViewBlock] = []
        self._global_masked = 0
        pitfalls = _preferred(self.store.facts_for([GLOBAL_SUBJECT], ["PITFALL"]))
        if pitfalls:
            items = sorted(pitfalls, key=lambda f: (severity(f) != "critical", pitfall_text(f)))
            lines = ["Global pitfalls:"]
            for f in items:
                sev = " (critical)" if severity(f) == "critical" else ""
                lines.append(f"  {WARN} Pitfall{sev}: {pitfall_text(f, self._free_global)}")
            out.append(ViewBlock(True, "\n".join(lines), "global_pitfalls", "Global pitfalls"))
        never = self.store.facts_for(None, ["NEVER_QUERY"])
        if never:
            eff = resolve(never, now=self.now, ttl_days=self.cfg.fact_ttl_days)
            lines = ["Never query:"]
            for key in sorted(eff):
                p = eff[key].primary
                if p is None or not isinstance(p.subject, ObjectRef):
                    continue
                table = p.subject.table_ref()
                reason = self._free(str(p.value.get("reason") or "no reason given"), table)
                self._global_masked += self._last_hits
                lines.append(f"  {WARN} {qname(p.subject.table_ref())} — {reason}{tag(p)}")
            if len(lines) > 1:
                out.append(ViewBlock(True, "\n".join(lines), "never_query", "Never query"))
        self._globals = out
        return out

    def _mode(self, table: ObjectRef | None = None) -> str:
        """The value policy of ``table``, or the default for text not tied to one table."""
        if table is None:
            return str(self.cfg.sampling.llm_values)
        return str(ValuePolicy.for_table(table, self.cfg.sampling))

    def _free(self, text: str, table: ObjectRef | None = None) -> str:
        """Free text masked under the policy (shapes and embedded names, never identifiers)."""
        self._last_hits = 0
        if self._mode(table) == "raw":
            return text
        masked, self._last_hits = mask_free_text(text)
        return masked

    def _free_global(self, text: str) -> str:
        out = self._free(text)
        self._global_masked += self._last_hits
        return out

    def table_budget(
        self, budget_tokens: int, counter: TokenCounter, *, tables: Sequence[ObjectRef] = ()
    ) -> int:
        """What remains for table blocks (separators included) after the always-on blocks.

        ``tables`` (the candidates the fitter may choose from) also reserves the join-path
        hazard warnings among them, which a view never drops.
        """
        if budget_tokens < 0:
            raise ValueError("budget_tokens must be >= 0")
        fixed = [
            *self.global_blocks(),
            ViewBlock(False, LEGEND, "legend", "Legend"),
            _not_shown(NOT_SHOWN_RESERVE),
        ]
        hazards = self.hazard_lines([t.table_ref() for t in dict.fromkeys(tables)])
        if hazards:
            fixed.append(ViewBlock(False, "\n".join([_PATHS_HEADER, *hazards]), "join_paths"))
        reserved = counter.count(SEPARATOR.join(b.text for b in fixed) + SEPARATOR)
        return max(0, budget_tokens - reserved)

    # ----- question-dependent blocks -----------------------------------------------------

    def glossary_items(self, shown: Sequence[ObjectRef]) -> list[str]:
        if self._glossary is None:
            self._glossary = self.store.facts_for([GLOBAL_SUBJECT], ["GLOSSARY", "METRIC"])
        wanted = set(shown)
        by_term: dict[str, list[Fact]] = {}
        for f in self._glossary:
            term = f.value.get("term") or f.value.get("name")
            if isinstance(term, str) and term.strip():
                by_term.setdefault(term.strip().casefold(), []).append(f)
        out: list[str] = []
        self._gloss_masked = 0
        for key in sorted(by_term):
            chosen = sorted(_preferred(by_term[key]), key=lambda f: f.id)
            if not chosen:
                continue
            f = chosen[0]
            maps = _refs(f.value.get("maps_to", f.value.get("tables")))
            if not wanted.intersection(maps):
                continue
            term = f.value.get("term") or f.value.get("name")
            kind = "metric " if f.kind.name == "METRIC" else ""
            targets = ", ".join(qname(r) for r in maps)
            term_t = self._free(str(term))
            hits = self._last_hits
            definition = self._free(str(f.value.get("definition")))
            self._gloss_masked += hits + self._last_hits
            out.append(f"  {kind}{term_t}: {definition} [tables: {targets}]{tag(f)}")
        return out

    def _scan(self, shown: Sequence[ObjectRef]) -> tuple[dict[str, list[str]], list[Path]]:
        """Hazard refusals (reason -> pairs) and the minimal best routes among ``shown``."""
        key_t = tuple(shown)
        cached = self._scans.get(key_t)
        if cached is not None:
            return cached
        hazards: dict[str, list[str]] = {}  # one line per reason, naming every pair it blocks
        routes: list[Path] = []
        if self.graph is not None and getattr(self.profile, "show_paths", True):
            gcfg = replace(self.cfg.graph, k_paths=1)
            shown_set = set(shown)
            seen: set[tuple[tuple[str, bool], ...]] = set()
            for i, a in enumerate(shown):
                for b in shown[i + 1 :]:
                    auto = None if self.rules is None else self.rules(a, b)
                    res = find_paths(self.graph, a, b, cfg=gcfg, automaton=auto)
                    if res.refusal in _HAZARD_REFUSALS:
                        why = res.explanation.replace(f"{a.canonical()} -> {b.canonical()} ", "", 1)
                        hazards.setdefault(why, []).append(f"{qname(a)} -> {qname(b)}")
                    if res.refusal is not None or not res.paths:
                        continue
                    p = res.paths[0]
                    if any(t in shown_set for t in p.tables[1:-1]):
                        continue  # composed of shorter routes already listed
                    key = tuple(h.key for h in p.hops)
                    rkey = tuple((k, not r) for k, r in reversed(key))
                    if key in seen or rkey in seen:
                        continue
                    seen.add(key)
                    routes.append(p)
        self._scans[key_t] = (hazards, routes)
        return hazards, routes

    def hazard_lines(self, shown: Sequence[ObjectRef]) -> list[str]:
        """``⚠ no safe route (...)`` lines: never dropped for budget (spec 25.6, 25.13)."""
        hazards, _ = self._scan(shown)
        return [
            f"  {WARN} no safe route ({'; '.join(pairs)}): {why}" for why, pairs in hazards.items()
        ]

    def path_items(self, shown: Sequence[ObjectRef], bag: DiagnosticBag) -> list[list[str]]:
        """The best route between each pair of shown tables that no other shown table splits."""
        _, routes = self._scan(shown)
        out: list[list[str]] = []
        for p in routes:
            snippet = None
            if self.dialect is not None:
                soft: dict[ObjectRef, str] = {}
                omitted: list[str] = []
                for t in sorted(set(p.tables), key=lambda r: r.canonical()):
                    pred, why = self._live_filter(t)
                    if pred is not None:
                        soft[t], _ = predicate_text(pred, self._mode(t))
                    elif why is not None:
                        omitted.append(f"-- live-rows filter for {qname(t)} {why}, omitted")
                snippet = render_join_snippet(p, self.dialect, soft, bag=bag)
                if snippet is not None and omitted:
                    snippet = "\n".join([snippet, *omitted])
            out.append(render_path(p, snippet=snippet))
        return out

    def _live_filter(self, ref: ObjectRef) -> tuple[str | None, str | None]:
        """``(predicate, None)`` when the live-rows filter may go into SQL, else
        ``(None, reason)``; ``(None, None)`` when the table has none.

        Only a VERIFIED predicate, or a HUMAN / DECLARED one that no check contradicted, is
        put into a snippet: an unchecked draft could silently drop live rows.
        """
        eff = self.knowledge(ref).get("SOFT_DELETE")
        p = None if eff is None else eff.primary
        pred = None if p is None else p.value.get("predicate")
        if p is None or not isinstance(pred, str) or not pred.strip():
            return None, None
        if p.status is Status.VERIFIED:
            return pred, None
        if p.status in (Status.CONTRADICTED, Status.WAIVED):
            return None, "contradicted"
        if p.provenance in (Provenance.HUMAN, Provenance.DECLARED) and p.status is not Status.ERROR:
            return pred, None
        return None, "unverified"

    # ----- the view ----------------------------------------------------------------------

    def view(
        self,
        question: str,
        tables: Sequence[tuple[ObjectRef, DetailLevel]],
        *,
        not_shown: int = 0,
        budget_tokens: int | None = None,
        counter: TokenCounter | None = None,
    ) -> ContextView:
        """Assemble the view for ``tables`` (already fitted, in rank order).

        Budget order: always-on blocks, tables and hazard warnings are always shown; then
        glossary lines; then join routes, while they fit. ``SG504`` names what overflowed when
        the mandatory part alone exceeds ``budget_tokens``.
        """
        counter = counter or CharTokenCounter()
        bag = DiagnosticBag()
        order = list(dict.fromkeys((r.table_ref(), lv) for r, lv in tables))
        refs = [r for r, _ in order]
        texts = [self.block_text(r, lv) for r, lv in order]
        if self.compact:
            lines = [ln for t in texts for ln in t.splitlines()]
            lines.sort(key=lambda ln: WARN not in ln)  # warnings first, rank order otherwise
            table_blocks = [ViewBlock(False, "\n".join(lines), "tables", "Tables")] if lines else []
        else:
            table_blocks = [
                ViewBlock(False, text, "table", qname(r))
                for (r, _), text in zip(order, texts, strict=True)
            ]
        stable = list(self.global_blocks())
        tail = _not_shown(not_shown)
        hazards = self.hazard_lines(refs)
        gloss_all = self.glossary_items(refs)
        routes_all = self.path_items(refs, bag)

        def assemble(gloss: list[str], routes: list[list[str]]) -> list[ViewBlock]:
            body: list[ViewBlock] = []
            if gloss:
                body.append(
                    ViewBlock(False, "\n".join(["Glossary:", *gloss]), "glossary", "Glossary")
                )
            body += table_blocks
            if hazards or routes:
                lines = [_PATHS_HEADER, *hazards, *(ln for r in routes for ln in r)]
                body.append(ViewBlock(False, "\n".join(lines), "join_paths", "Join paths"))
            body.append(tail)
            # the legend's presence depends on the question, so it is not a stable block
            legend = needs_legend([b.text for b in (*stable, *body)])
            head = [*stable, ViewBlock(False, LEGEND, "legend", "Legend")] if legend else stable
            return [*head, *body]

        def size(blocks: list[ViewBlock]) -> int:
            return counter.count(SEPARATOR.join(b.text for b in blocks))

        def fits(blocks: list[ViewBlock]) -> bool:
            return budget_tokens is None or size(blocks) <= budget_tokens

        gloss: list[str] = []
        for item in gloss_all:
            if not fits(assemble([*gloss, item], [])):
                break
            gloss.append(item)
        routes: list[list[str]] = []
        for route in routes_all:
            if not fits(assemble(gloss, [*routes, route])):
                break
            routes.append(route)
        blocks = assemble(gloss, routes)
        # the last step (spec 11): one shape sweep over every block that leaves
        masked = self._global_masked + self._gloss_masked
        masked += sum(analyse(self.knowledge(r)).masked for r in refs)
        if self._mode() != "raw":
            swept: list[ViewBlock] = []
            for b in blocks:
                text, hits = outbound(b.text, "masked")
                masked += hits
                swept.append(b if hits == 0 else replace(b, text=text))
            blocks = swept
        if masked:
            bag.add(
                Diagnostic(
                    "SG505",
                    Severity.INFO,
                    f"value policy: {masked} value(s) or text span(s) masked in this view",
                )
            )
        if not fits(blocks):
            fixed = [b for b in blocks if b.kind not in ("table", "tables", "join_paths")]
            hz = counter.count("\n".join([_PATHS_HEADER, *hazards])) if hazards else 0
            tb = sum(counter.count(b.text) for b in table_blocks)
            bag.add(
                Diagnostic(
                    "SG504",
                    Severity.WARNING,
                    f"view is {size(blocks)} tokens, over its budget of {budget_tokens}: "
                    f"always-on blocks {size(fixed)}, tables {tb}, join-path hazards {hz}",
                    hint="raise budget_tokens, or fit tables with ViewRenderer.table_budget",
                )
            )
        diags = list({(d.code, d.message): d for d in bag}.values())
        return ContextView.build(question, blocks, tables=refs, counter=counter, diagnostics=diags)


def build_view(
    store: Store,
    question: str,
    tables: Sequence[tuple[ObjectRef, DetailLevel]],
    *,
    now: datetime,
    cfg: Config,
    profile: str | Profile = "agent",
    graph: Graph | None = None,
    not_shown: int = 0,
    budget_tokens: int | None = None,
    counter: TokenCounter | None = None,
) -> ContextView:
    """One-shot :meth:`ViewRenderer.view`."""
    r = ViewRenderer(store, now=now, cfg=cfg, profile=profile, graph=graph)
    return r.view(
        question, tables, not_shown=not_shown, budget_tokens=budget_tokens, counter=counter
    )


__all__ = [
    "SEPARATOR",
    "ContextView",
    "TableKnowledge",
    "ViewBlock",
    "ViewRenderer",
    "analyse",
    "build_view",
    "collect",
]
