"""The FactLang seam for rendering (spec 9.7, 5.2): join snippets and filter validation.

One of the three modules that import ``factlang``. Every FactLang name is looked up at call
time: when the installed FactLang lacks ``fl.sql.render_join`` a join snippet is simply not
offered (``None`` plus an ``SG501`` info), and when it lacks ``fl.parse_filter`` filters get the
overlays' syntax-only check (``SG503`` info says so). Nothing here executes SQL: a snippet is
advisory text for the agent.
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from typing import Any

from schemaguru.diagnostics import Diagnostic, DiagnosticBag, Severity
from schemaguru.graph.search import Path
from schemaguru.measure import flbridge
from schemaguru.model.refs import ObjectRef
from schemaguru.overlays.schema import check_filter_syntax


def _capable(name: str) -> bool:
    return name in flbridge.capabilities()


def _ident(fl_refs: Any, text: str) -> Any:
    return fl_refs.Ident(text, True)  # names are stored exactly as the catalog returns them


def _table(fl_refs: Any, ref: ObjectRef) -> Any:
    return fl_refs.TableRef(
        None if ref.catalog is None else _ident(fl_refs, ref.catalog),
        None if ref.schema is None else _ident(fl_refs, ref.schema),
        _ident(fl_refs, ref.table),
    )


def _column(fl_refs: Any, ref: ObjectRef) -> Any:
    assert ref.column is not None
    return fl_refs.ColumnRef(_table(fl_refs, ref.table_ref()), _ident(fl_refs, ref.column))


def render_join_snippet(
    path: Path,
    dialect: str,
    soft_delete: Mapping[ObjectRef, str],
    *,
    bag: DiagnosticBag | None = None,
) -> str | None:
    """A dialect-correct ``FROM ... JOIN ... ON ...`` for ``path`` via ``fl.sql.render_join``.

    Each table's effective soft-delete predicate (``soft_delete``, FactLang filter text) is passed
    as its filter. Returns ``None`` (and an ``SG501`` info in ``bag``) when the installed FactLang
    cannot render joins or rejects this path; never raises on data.
    """
    if not _capable("render_join"):
        if bag is not None:
            bag.add(
                Diagnostic(
                    "SG501",
                    Severity.INFO,
                    "join snippet not rendered: the installed FactLang lacks fl.sql.render_join",
                    hint="the join keys are still listed; upgrade FactLang for SQL snippets",
                )
            )
        return None
    try:
        fl = importlib.import_module("factlang")
        sql = importlib.import_module("factlang.sql")
        refs = importlib.import_module("factlang.semantics.refs")
        edges = []
        aliases: dict[int, str] = {}
        for i, hop in enumerate(path.hops):
            join = hop.edge.join
            left = [_column(refs, c) for c in join.left]
            right = [_column(refs, c) for c in join.right]
            pairs = list(zip(left, right, strict=True))
            if hop.reverse:
                pairs = [(r, lf) for lf, r in pairs]
            edges.append(
                refs.EdgeRef(pairs[0][0], pairs[0][1])
                if len(pairs) == 1
                else refs.EdgeRef(tuple(p[0] for p in pairs), tuple(p[1] for p in pairs))
            )
            if path.revisits:
                aliases[i + 1] = f"t{i + 1}"
        filters: dict[Any, Any] = {}
        for ref, text in sorted(soft_delete.items(), key=lambda kv: kv[0].canonical()):
            if ref in path.tables:
                expr, diags = fl.parse_filter(text)
                if expr is not None and not [d for d in diags if _is_error(d)]:
                    filters[_table(refs, ref)] = expr
        kwargs: dict[str, Any] = {"dialect": dialect, "filters": filters or None}
        if aliases:
            kwargs["aliases"] = aliases
        fragment = sql.render_join(edges, **kwargs)
        return str(fragment.inline())
    except Exception as exc:  # a FactLang refusal costs this one snippet, never the view
        if bag is not None:
            bag.add(
                Diagnostic(
                    "SG501",
                    Severity.INFO,
                    f"join snippet not rendered: FactLang refused the path ({exc})",
                )
            )
        return None


def _is_error(d: Any) -> bool:
    sev = getattr(d, "severity", None)
    return str(getattr(sev, "value", sev)).upper() == "ERROR"


def validate_filter(text: str, dialect: str) -> DiagnosticBag:
    """Check FactLang filter text (spec 4.3 ``SOFT_DELETE``, 14.2): ``SG502`` errors if invalid.

    Uses ``fl.parse_filter`` when installed; otherwise the overlays' syntax-only check, reported
    with an ``SG503`` info so a caller knows the text was not fully parsed.
    """
    bag = DiagnosticBag()
    if _capable("parse_filter"):
        try:
            fl = importlib.import_module("factlang")
            expr, diags = fl.parse_filter(text)
        except Exception as exc:
            bag.add(Diagnostic("SG502", Severity.ERROR, f"invalid filter {text!r}: {exc}"))
            return bag
        problems = [d for d in diags if _is_error(d)]
        for d in problems:
            msg = getattr(d, "message", str(d))
            bag.add(Diagnostic("SG502", Severity.ERROR, f"invalid filter {text!r}: {msg}"))
        if expr is None and not problems:
            bag.add(Diagnostic("SG502", Severity.ERROR, f"invalid filter {text!r}"))
        return bag
    bag.add(
        Diagnostic(
            "SG503",
            Severity.INFO,
            f"filter checked for syntax only ({dialect}): the installed FactLang lacks "
            "fl.parse_filter",
        )
    )
    problem = check_filter_syntax(text)
    if problem is not None:
        bag.add(Diagnostic("SG502", Severity.ERROR, f"invalid filter {text!r}: {problem}"))
    return bag
