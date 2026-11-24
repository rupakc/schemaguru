"""Compile a linear identity chain into a pair waypoint rule (spec 4.6, 9.5).

A chain is linear when its join steps walk a simple path: each join shares exactly one table
with the next and no table is visited twice. Filter and note steps do not constrain the route.
Each join is matched to the graph edge(s) with the same columns, in either orientation, and the
requirement is written the way the graph declares it (``edge()`` predicates match exactly);
when the graph declares the columns both ways, the requirement accepts either edge.
A chain that cannot be compiled (no joins, not linear, disconnected, or a join the graph does
not hold) compiles to ``None`` and is reported as an ``SG605`` warning - never dropped silently.
"""

from __future__ import annotations

from schemaguru.diagnostics import Diagnostic, DiagnosticBag, Severity
from schemaguru.graph.search import Graph
from schemaguru.graph.waypoints import edge_text
from schemaguru.model.chains import Chain
from schemaguru.model.joins import JoinRef
from schemaguru.model.refs import ObjectRef
from schemaguru.overlays.schema import WaypointSpec


def _graph_orientations(join: JoinRef, graph: Graph) -> list[JoinRef]:
    """Every declared orientation of ``join``'s columns, ``cast_to`` dropped, deduplicated."""
    found: list[JoinRef] = []
    for e in graph.edges:
        jr = e.join
        if (jr.left, jr.right) in ((join.left, join.right), (join.right, join.left)):
            plain = JoinRef(jr.left, jr.right)
            if plain not in found:
                found.append(plain)
    return sorted(found, key=lambda j: j.canonical())


def _ends(join: JoinRef) -> tuple[ObjectRef, ObjectRef]:
    return join.left[0].table_ref(), join.right[0].table_ref()


def _walk(joins: list[JoinRef]) -> list[ObjectRef] | str:
    """The tables the joins visit in order, or why they do not form a simple path."""
    a, b = _ends(joins[0])
    if len(joins) > 1:
        nxt = set(_ends(joins[1]))
        if (a in nxt) == (b in nxt):
            return f"step 2 ({joins[1].canonical()}) does not continue step 1"
        if a in nxt:
            a, b = b, a
    walk = [a, b]
    for i, jr in enumerate(joins[1:], start=2):
        x, y = _ends(jr)
        if x == walk[-1]:
            walk.append(y)
        elif y == walk[-1]:
            walk.append(x)
        else:
            return f"step {i} ({jr.canonical()}) is disconnected from {walk[-1].canonical()}"
    if len(set(walk)) != len(walk):
        return "the joins revisit a table, so the chain is not linear"
    return walk


def compile_chain(
    chain: Chain, graph: Graph, *, bag: DiagnosticBag | None = None
) -> WaypointSpec | None:
    where = f"chain:{chain.name}"

    def fail(why: str) -> None:
        if bag is not None:
            bag.add(
                Diagnostic(
                    "SG605",
                    Severity.WARNING,
                    f"chain {chain.name!r} cannot be compiled into a waypoint rule: {why}",
                    location=where,
                    hint="fix the chain steps; until then it does not constrain any route",
                )
            )

    joins = [s for s in chain.steps if isinstance(s, JoinRef)]
    if not joins:
        fail("it has no join steps")
        return None
    walk = _walk(joins)
    if isinstance(walk, str):
        fail(walk)
        return None
    terms: list[str] = []
    for jr in joins:
        oriented = _graph_orientations(jr, graph)
        if not oriented:
            fail(f"the graph holds no usable join {jr.canonical()} in either direction")
            return None
        terms.append(" | ".join(edge_text(o) for o in oriented))
    return WaypointSpec("pair", (walk[0], walk[-1]), tuple(terms), (), chain.because, where)
