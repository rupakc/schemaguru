"""Waypoint rules compiled into a bitmask automaton with a dead state (spec 9.5).

A rule constrains routes of the form ``any* p any*``: a *requirement* ``p`` is met when some
element of the route matches it; a *prohibition* kills the route when any element matches it.
A route's elements are its tables (the start table and every hop's arrival table) and its edges.

Predicates, matched exactly (no globbing, no case folding):

* ``node(t)`` - the table ``t``;
* ``col(t.c)`` - any edge with column ``t.c`` on either side;
* ``edge(a.x->b.y)`` - the edge declared ``a.x -> b.y`` (composite: ``a.x, a.y->b.x, b.y``),
  in the orientation the graph declares it, whichever way it is traversed (``cast_to`` is
  not part of the predicate);
* ``any`` - every element.

Pair scope (``pair: [a, b]``) may require and forbid; it governs searches between ``a`` and
``b`` in either direction. Node scope (``node: t``) may only forbid; it governs every search,
except that it is withheld when ``t`` is a search endpoint, and a forbidden ``node(x)`` is
withheld when ``x`` is one. At most :data:`MAX_REQUIREMENTS` requirements per rule.

Fail closed: a rule that cannot be compiled (``SG602`` bad predicate, ``require`` on a node
rule, empty ``because``; ``SG603`` too many requirements) is reported and makes every search
it governs refuse with ``WAYPOINT_UNSATISFIABLE`` - it is never silently dropped.
Emptiness is decided offline from the predicates alone (:meth:`RuleAutomaton.is_empty`);
a rule that is satisfiable in principle but not in this graph surfaces as ``RULE_REJECTS_ALL``.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

from schemaguru.diagnostics import Diagnostic, DiagnosticBag, Severity
from schemaguru.graph.edges import Edge
from schemaguru.graph.search import Hop
from schemaguru.model.joins import JoinRef
from schemaguru.model.refs import ObjectRef
from schemaguru.overlays.schema import WaypointSpec

MAX_REQUIREMENTS = 16
DEAD = -1  # absorbing: a prohibited element was seen

PredicateKind = Literal["node", "col", "edge", "any"]


@dataclass(frozen=True)
class Predicate:
    kind: PredicateKind
    table: ObjectRef | None = None  # node
    column: ObjectRef | None = None  # col
    join: JoinRef | None = None  # edge (cast_to always None)

    def text(self) -> str:
        if self.kind == "node" and self.table is not None:
            return f"node({self.table.canonical()})"
        if self.kind == "col" and self.column is not None:
            return f"col({self.column.canonical()})"
        if self.kind == "edge" and self.join is not None:
            return edge_text(self.join)
        return "any"

    def matches_table(self, table: ObjectRef) -> bool:
        return self.kind == "any" or (self.kind == "node" and self.table == table)

    def matches_edge(self, edge: Edge) -> bool:
        if self.kind == "any":
            return True
        jr = edge.join
        if self.kind == "col":
            return self.column in jr.left or self.column in jr.right
        if self.kind == "edge" and self.join is not None:
            return self.join.left == jr.left and self.join.right == jr.right
        return False

    def matches_hop(self, hop: Hop) -> bool:
        """The hop's elements: its edge and its arrival table."""
        return self.matches_edge(hop.edge) or self.matches_table(hop.end)

    def implies(self, other: Predicate) -> bool:
        """Every route holding an element matching ``self`` holds one matching ``other``."""
        if other.kind == "any":
            return True
        if self.kind == "node":
            return other.kind == "node" and other.table == self.table
        if self.kind == "col" and self.column is not None:
            if other.kind == "col":
                return other.column == self.column
            return other.kind == "node" and other.table == self.column.table_ref()
        if self.kind == "edge" and self.join is not None:
            jr = self.join
            if other.kind == "edge" and other.join is not None:
                return other.join.left == jr.left and other.join.right == jr.right
            if other.kind == "col":
                return other.column in jr.left or other.column in jr.right
            return other.kind == "node" and other.table in (
                jr.left[0].table_ref(),
                jr.right[0].table_ref(),
            )
        return False


def edge_text(join: JoinRef) -> str:
    left = ", ".join(c.canonical() for c in join.left)
    right = ", ".join(c.canonical() for c in join.right)
    return f"edge({left}->{right})"


def _split_outside_quotes(text: str, sep: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    quoted = False
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            quoted = not quoted  # a doubled "" toggles twice: still correct
            buf.append(ch)
            i += 1
        elif not quoted and text.startswith(sep, i):
            parts.append("".join(buf))
            buf = []
            i += len(sep)
        else:
            buf.append(ch)
            i += 1
    if quoted:
        raise ValueError(f"unterminated quote in {text!r}")
    parts.append("".join(buf))
    return parts


def _columns(text: str) -> tuple[ObjectRef, ...]:
    return tuple(
        ObjectRef.parse(p.strip(), has_column=True) for p in _split_outside_quotes(text, ",")
    )


def parse_predicate(token: str) -> Predicate:
    """Parse one predicate; anything else raises :class:`ValueError` (callers fail closed)."""
    text = token.strip()
    if text == "any":
        return Predicate("any")
    open_at = text.find("(")
    if open_at < 0 or not text.endswith(")"):
        raise ValueError(f"not a predicate: {token!r} (use node(t), col(t.c), edge(a.x->b.y), any)")
    kind, body = text[:open_at].strip(), text[open_at + 1 : -1].strip()
    if not body:
        raise ValueError(f"empty predicate argument in {token!r}")
    if kind == "node":
        ref = ObjectRef.parse(body)
        if ref.column is not None:
            raise ValueError(f"node() takes a table, not a column, in {token!r}")
        return Predicate("node", table=ref)
    if kind == "col":
        col = ObjectRef.parse(body, has_column=True)
        return Predicate("col", column=col)
    if kind == "edge":
        sides = _split_outside_quotes(body, "->")
        if len(sides) != 2:
            raise ValueError(f"edge needs exactly one '->' in {token!r}")
        return Predicate("edge", join=JoinRef(_columns(sides[0]), _columns(sides[1])))
    raise ValueError(f"unknown predicate {kind!r} in {token!r}")


# --- validation ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Compiled:
    spec: WaypointSpec
    require: tuple[Predicate, ...]
    forbid: tuple[Predicate, ...]


def _compile_one(spec: WaypointSpec) -> _Compiled | Diagnostic:
    def bad(code: str, why: str, hint: str) -> Diagnostic:
        return Diagnostic(code, Severity.ERROR, why, location=spec.source, hint=hint)

    fail = "the searches this rule governs are refused until it is fixed"
    if not spec.because.strip():
        return bad("SG602", "waypoint rule has no 'because'", fail)
    if spec.scope == "node" and spec.require:
        return bad("SG602", "a node waypoint rule may only forbid, not require", fail)
    if len(spec.require) > MAX_REQUIREMENTS:
        return bad(
            "SG603",
            f"waypoint rule has {len(spec.require)} requirements; at most "
            f"{MAX_REQUIREMENTS} are allowed",
            f"split it into several rules; {fail}",
        )
    try:
        req = tuple(parse_predicate(p) for p in spec.require)
        forb = tuple(parse_predicate(p) for p in spec.forbid)
    except ValueError as exc:
        return bad("SG602", f"waypoint rule cannot be parsed: {exc}", fail)
    return _Compiled(spec, req, forb)


def validate_rules(specs: Iterable[WaypointSpec]) -> list[Diagnostic]:
    """``SG602``/``SG603`` errors for every rule that cannot be compiled (run at load)."""
    return [d for d in map(_compile_one, specs) if isinstance(d, Diagnostic)]


# --- automaton -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class RuleAutomaton:
    """States are ints: a bitmask of requirements met, or :data:`DEAD`.

    Implements :class:`schemaguru.graph.search.Automaton`.
    """

    src: ObjectRef
    dst: ObjectRef
    require: tuple[Predicate, ...]
    forbid: tuple[Predicate, ...]
    broken: tuple[str, ...] = ()  # SG602/SG603 rules governing this pair: fail closed

    def _absorb(self, state: int, table: ObjectRef | None, hop: Hop | None) -> int:
        if state == DEAD:
            return DEAD
        for f in self.forbid:
            if (hop is not None and f.matches_hop(hop)) or (
                table is not None and f.matches_table(table)
            ):
                return DEAD
        for i, r in enumerate(self.require):
            if (hop is not None and r.matches_hop(hop)) or (
                table is not None and r.matches_table(table)
            ):
                state |= 1 << i
        return state

    @property
    def start(self) -> int:
        return self._absorb(0, self.src, None)

    def step(self, state: int, hop: Hop) -> int:
        return self._absorb(state, None, hop)

    def accepting(self, state: int) -> bool:
        return state != DEAD and state == (1 << len(self.require)) - 1

    def why_empty(self) -> str | None:
        """Why no route can ever be accepted, or ``None`` when some route might be."""
        if self.broken:
            return "; ".join(self.broken)
        for end in (self.src, self.dst):
            if any(f.matches_table(end) for f in self.forbid):
                return f"the endpoint {end.canonical()} is itself forbidden"
        for r in self.require:
            for f in self.forbid:
                if r.implies(f):
                    return f"requirement {r.text()} always meets prohibition {f.text()}"
        return None

    def is_empty(self) -> bool:
        return self.why_empty() is not None


def _governs(spec: WaypointSpec, src: ObjectRef, dst: ObjectRef) -> bool:
    if spec.scope == "pair":
        return {x.table_ref() for x in spec.tables} == {src, dst} and len(spec.tables) == 2
    return bool(spec.tables) and spec.tables[0].table_ref() not in (src, dst)


def compile_rules(
    specs: Sequence[WaypointSpec],
    src: ObjectRef,
    dst: ObjectRef,
    *,
    bag: DiagnosticBag | None = None,
) -> RuleAutomaton:
    """One automaton for the search ``src`` -> ``dst`` from every rule governing it.

    Diagnostics for *every* malformed rule go to ``bag``; a malformed rule that governs this
    search makes the automaton empty (fail closed).
    """
    s, d = src.table_ref(), dst.table_ref()
    require: list[Predicate] = []
    forbid: list[Predicate] = []
    broken: list[str] = []
    for spec in specs:
        out = _compile_one(spec)
        if isinstance(out, Diagnostic):
            if bag is not None:
                bag.add(out)
            if _governs(spec, s, d):
                broken.append(f"{out.code} at {spec.source}: {out.message}")
            continue
        if not _governs(spec, s, d):
            continue
        require += out.require
        for f in out.forbid:
            if spec.scope == "node" and f.kind == "node" and f.table in (s, d):
                continue  # withheld at a search endpoint
            forbid.append(f)
    return RuleAutomaton(s, d, tuple(require), tuple(forbid), tuple(broken))


# --- lint ----------------------------------------------------------------------------------------


def lint_rules(specs: Iterable[WaypointSpec], edges: Iterable[Edge]) -> list[Diagnostic]:
    """``SG604`` for every prohibition that no edge (or edge endpoint) can match.

    Malformed rules are skipped here; :func:`validate_rules` reports them.
    """
    edge_list = list(edges)
    tables = {e.left_table for e in edge_list} | {e.right_table for e in edge_list}
    out: list[Diagnostic] = []
    for spec in specs:
        compiled = _compile_one(spec)
        if isinstance(compiled, Diagnostic):
            continue
        for f in compiled.forbid:
            live = any(f.matches_table(x) for x in tables) or any(
                f.matches_edge(e) for e in edge_list
            )
            if not live:
                out.append(
                    Diagnostic(
                        "SG604",
                        Severity.WARNING,
                        f"waypoint prohibition {f.text()} matches no edge in the graph",
                        location=spec.source,
                        hint="check the spelling and, for edge(), the declared orientation",
                    )
                )
    return out
