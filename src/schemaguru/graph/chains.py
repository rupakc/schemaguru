"""Compile a linear identity chain into a pair waypoint rule (spec 4.6, 9.5).

A chain is linear when its join steps walk a simple path: each join shares exactly one table
with the next and no table is visited twice. Filter and note steps do not constrain the route.
Each join is matched to the graph edge with the same columns, in either orientation, and the
requirement is written the way the graph declares it (``edge()`` predicates match exactly).
A chain that is not linear, has no joins, or names a join the graph does not hold, compiles to
``None``.
"""

from __future__ import annotations

from schemaguru.graph.search import Graph
from schemaguru.graph.waypoints import edge_text
from schemaguru.model.chains import Chain
from schemaguru.model.joins import JoinRef
from schemaguru.model.refs import ObjectRef
from schemaguru.overlays.schema import WaypointSpec


def _graph_orientation(join: JoinRef, graph: Graph) -> JoinRef | None:
    for e in graph.edges:
        jr = e.join
        if (jr.left, jr.right) == (join.left, join.right) or (jr.left, jr.right) == (
            join.right,
            join.left,
        ):
            return JoinRef(jr.left, jr.right)
    return None


def _ends(join: JoinRef) -> tuple[ObjectRef, ObjectRef]:
    return join.left[0].table_ref(), join.right[0].table_ref()


def compile_chain(chain: Chain, graph: Graph) -> WaypointSpec | None:
    joins = [s for s in chain.steps if isinstance(s, JoinRef)]
    if not joins:
        return None
    oriented: list[JoinRef] = []
    for jr in joins:
        g = _graph_orientation(jr, graph)
        if g is None:
            return None
        oriented.append(g)
    # Walk the tables: start at the end of the first join not shared with the second.
    a, b = _ends(joins[0])
    if len(joins) > 1:
        nxt = set(_ends(joins[1]))
        if (a in nxt) == (b in nxt):
            return None  # shares no table, or both
        if a in nxt:
            a, b = b, a
    walk = [a, b]
    for jr in joins[1:]:
        x, y = _ends(jr)
        if x == walk[-1]:
            walk.append(y)
        elif y == walk[-1]:
            walk.append(x)
        else:
            return None  # disconnected
    if len(set(walk)) != len(walk):
        return None  # revisits a table: not linear
    return WaypointSpec(
        "pair",
        (walk[0], walk[-1]),
        tuple(edge_text(jr) for jr in oriented),
        (),
        chain.because,
        f"chain:{chain.name}",
    )
