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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime

from schemaguru._util.hashing import stable_hash
from schemaguru._util.timeutil import require_aware
from schemaguru._util.tokens import CharTokenCounter, TokenCounter
from schemaguru.config import Config
from schemaguru.diagnostics import Diagnostic, DiagnosticBag, Severity
from schemaguru.graph.search import Graph, Path, Refusal, find_paths
from schemaguru.knowledge.resolve import resolve
from schemaguru.model.detail import DetailLevel
from schemaguru.model.facts import Fact, Provenance, Status, Waiver
from schemaguru.model.refs import ObjectRef
from schemaguru.overlays.schema import GLOBAL_SUBJECT
from schemaguru.render import formats
from schemaguru.render.fragments import render_join_snippet
from schemaguru.render.knowledge import TableKnowledge, collect
from schemaguru.render.legend import LEGEND, WARN, needs_legend
from schemaguru.render.profiles import Profile, get_profile
from schemaguru.render.table_block import analyse, pitfall_text, render_path, severity, tag
from schemaguru.store.protocol import Store

SEPARATOR = "\n\n"
# The "Not shown" line is reserved at this count, so the real line always fits.
NOT_SHOWN_RESERVE = 999_999
_PATHS_HEADER = "Join paths (measured joins; join keys are advisory):"
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

    @property
    def text(self) -> str:
        return SEPARATOR.join(b.text for b in self.blocks)

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
    ) -> None:
        self.store = store
        self.now = require_aware(now)
        self.cfg = cfg
        self.profile = get_profile(profile) if isinstance(profile, str) else profile
        self.graph = graph
        self.waivers = tuple(waivers)
        self.dialect = dialect
        self._knowledge: dict[ObjectRef, TableKnowledge] = {}
        self._joins: list[Fact] | None = None
        self._globals: list[ViewBlock] | None = None
        self._glossary: list[Fact] | None = None

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
        pitfalls = _preferred(self.store.facts_for([GLOBAL_SUBJECT], ["PITFALL"]))
        if pitfalls:
            items = sorted(pitfalls, key=lambda f: (severity(f) != "critical", pitfall_text(f)))
            lines = ["Global pitfalls:"]
            for f in items:
                sev = " (critical)" if severity(f) == "critical" else ""
                lines.append(f"  {WARN} Pitfall{sev}: {pitfall_text(f)}")
            out.append(ViewBlock(True, "\n".join(lines), "global_pitfalls", "Global pitfalls"))
        never = self.store.facts_for(None, ["NEVER_QUERY"])
        if never:
            eff = resolve(never, now=self.now, ttl_days=self.cfg.fact_ttl_days)
            lines = ["Never query:"]
            for key in sorted(eff):
                p = eff[key].primary
                if p is None or not isinstance(p.subject, ObjectRef):
                    continue
                reason = p.value.get("reason") or "no reason given"
                lines.append(f"  {WARN} {p.subject.table_ref().canonical()} — {reason}{tag(p)}")
            if len(lines) > 1:
                out.append(ViewBlock(True, "\n".join(lines), "never_query", "Never query"))
        self._globals = out
        return out

    def table_budget(self, budget_tokens: int, counter: TokenCounter) -> int:
        """What remains for table blocks (separators included) after the always-on blocks."""
        if budget_tokens < 0:
            raise ValueError("budget_tokens must be >= 0")
        fixed = [
            *self.global_blocks(),
            ViewBlock(True, LEGEND, "legend", "Legend"),
            _not_shown(NOT_SHOWN_RESERVE),
        ]
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
            targets = ", ".join(r.canonical() for r in maps)
            out.append(f"  {kind}{term}: {f.value.get('definition')} [tables: {targets}]{tag(f)}")
        return out

    def path_items(
        self, shown: Sequence[ObjectRef], bag: DiagnosticBag
    ) -> tuple[list[str], list[list[str]]]:
        """Refusal warnings, then the best route between each pair of shown tables.

        A route another shown table splits is left out (it is composed of listed routes). A pair
        the graph refuses for a hazard (hub pivot, waypoint rules) gets a ``⚠`` line, so two
        listed routes are never chained through a hub silently.
        """
        if self.graph is None or not getattr(self.profile, "show_paths", True):
            return [], []
        gcfg = replace(self.cfg.graph, k_paths=1)
        shown_set = set(shown)
        hazards: dict[str, list[str]] = {}  # one line per reason, naming every pair it blocks
        routes: list[Path] = []
        seen: set[tuple[tuple[str, bool], ...]] = set()
        for i, a in enumerate(shown):
            for b in shown[i + 1 :]:
                res = find_paths(self.graph, a, b, cfg=gcfg)
                if res.refusal in _HAZARD_REFUSALS:
                    pair = f"{a.canonical()} -> {b.canonical()}"
                    why = res.explanation.replace(f"{pair} ", "", 1)
                    hazards.setdefault(why, []).append(pair)
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
        out: list[list[str]] = []
        for p in routes:
            snippet = None
            if self.dialect is not None:
                soft = {t: s for t in set(p.tables) if (s := self._soft_delete(t)) is not None}
                snippet = render_join_snippet(p, self.dialect, soft, bag=bag)
            out.append(render_path(p, snippet=snippet))
        warnings = [
            f"  {WARN} no safe route ({'; '.join(pairs)}): {why}" for why, pairs in hazards.items()
        ]
        return warnings, out

    def _soft_delete(self, ref: ObjectRef) -> str | None:
        eff = self.knowledge(ref).get("SOFT_DELETE")
        p = None if eff is None else eff.primary
        pred = None if p is None else p.value.get("predicate")
        return pred if isinstance(pred, str) and pred.strip() else None

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

        With ``budget_tokens``, the glossary and join-path blocks take only what is left; if
        even the always-on blocks exceed the budget, ``SG504`` says so.
        """
        counter = counter or CharTokenCounter()
        bag = DiagnosticBag()
        order = list(dict.fromkeys((r.table_ref(), lv) for r, lv in tables))
        refs = [r for r, _ in order]
        texts = [self.block_text(r, lv) for r, lv in order]
        if self.compact:
            table_blocks = [ViewBlock(False, "\n".join(texts), "tables", "Tables")] if texts else []
        else:
            table_blocks = [
                ViewBlock(False, text, "table", r.canonical())
                for (r, _), text in zip(order, texts, strict=True)
            ]
        stable = list(self.global_blocks())
        tail = _not_shown(not_shown)
        gloss_all = self.glossary_items(refs)
        path_warnings, paths_all = self.path_items(refs, bag)

        def assemble(gloss: list[str], paths: list[list[str]] | None) -> list[ViewBlock]:
            body: list[ViewBlock] = []
            if gloss:
                body.append(
                    ViewBlock(False, "\n".join(["Glossary:", *gloss]), "glossary", "Glossary")
                )
            body += table_blocks
            if paths is not None and (paths or path_warnings):
                lines = [_PATHS_HEADER, *path_warnings, *(ln for p in paths for ln in p)]
                body.append(ViewBlock(False, "\n".join(lines), "join_paths", "Join paths"))
            body.append(tail)
            legend = needs_legend([b.text for b in (*stable, *body)])
            head = [*stable, ViewBlock(True, LEGEND, "legend", "Legend")] if legend else stable
            return [*head, *body]

        def fits(blocks: list[ViewBlock]) -> bool:
            if budget_tokens is None:
                return True
            return counter.count(SEPARATOR.join(b.text for b in blocks)) <= budget_tokens

        gloss: list[str] = []
        for item in gloss_all:
            if not fits(assemble([*gloss, item], None)):
                break
            gloss.append(item)
        # the refusal warnings come with the first route or not at all
        paths: list[list[str]] | None = None
        if fits(assemble(gloss, [])):
            paths = []
            for route in paths_all:
                if not fits(assemble(gloss, [*paths, route])):
                    break
                paths.append(route)
        blocks = assemble(gloss, paths)
        if not fits(blocks):
            bag.add(
                Diagnostic(
                    "SG504",
                    Severity.WARNING,
                    f"view exceeds its budget of {budget_tokens} tokens: the always-on blocks "
                    "and the fitted tables do not fit",
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
