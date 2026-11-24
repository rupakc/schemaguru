"""The FactLang seam for rendering (spec 9.7, 5.2): join snippets and filter validation.

One of the three modules that import ``factlang``. Every FactLang name is looked up at call
time: when the installed FactLang lacks ``fl.sql.render_join`` a join snippet is simply not
offered (``None`` plus an ``SG501`` info), and when it lacks ``fl.parse_filter`` filters get the
overlays' syntax-only check (``SG503`` info says so). Nothing here executes SQL: a snippet is
advisory text for the agent.
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Iterable, Mapping
from typing import Any

from factlang.syntax import FILTER_KEYWORDS  # public since FactLang 0.1.0

from schemaguru._util.sqltext import scan
from schemaguru.diagnostics import Diagnostic, DiagnosticBag, Severity
from schemaguru.graph.search import Path
from schemaguru.measure import flbridge
from schemaguru.model.refs import ObjectRef
from schemaguru.overlays.schema import check_filter_syntax
from schemaguru.render.table_block import path_aliases, qname

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
# FILTER_KEYWORDS (FactLang's, imported above): the words its filter lexer reads as keywords,
# upper case; never a column reference


def quote_catalog_names(text: str, columns: Iterable[str]) -> tuple[str, list[str]]:
    """``text`` (FactLang filter source) with every bare column reference that has a UNIQUE
    catalog match written quoted in the catalog's exact case, on every dialect, plus the
    identifiers left unquoted because their match was ambiguous.

    Unquoted, a name is case-folded by the warehouse (Postgres to lower, Snowflake/Oracle to
    upper), so ``is_deleted`` would become ``IS_DELETED`` on Snowflake: invalid, or another
    column. An exact-case match wins; else a unique case-insensitive one; several that differ
    only in case (``IsDeleted``/``ISDELETED``) are ambiguous and left alone. FactLang keywords
    (as it lexes them, upper case) are never quoted, literals and quoted names never touched, and
    in ``t.isDeleted`` only the column part (after the ``.``) is a candidate."""
    by_fold: dict[str, list[str]] = {}
    for name in columns:
        by_fold.setdefault(name.lower(), []).append(name)
    chars = scan(text)
    out: list[str] = []
    ambiguous: list[str] = []
    i = 0
    while i < len(text):
        ch, lit = chars[i]
        m = None if lit else _IDENT.match(text, i)
        if m is None or (i > 0 and _IDENT.match(text[i - 1]) is not None):
            out.append(ch)
            i += 1
            continue
        word, end = m.group(0), m.end()
        qualifier = end < len(text) and text[end] == "."  # ``t`` in ``t.col``: not a column
        if qualifier or word in FILTER_KEYWORDS or any(lit for _, lit in chars[i:end]):
            out.append(word)
            i = end
            continue
        matches = by_fold.get(word.lower(), [])
        exact = [n for n in matches if n == word]
        names = exact or matches
        if len(names) == 1:
            out.append('"' + names[0].replace('"', '""') + '"')
        else:
            if len(names) > 1:
                ambiguous.append(word)
            out.append(word)
        i = end
    return "".join(out), ambiguous


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
    catalog: Any | None = None,
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
    if len(set(path.tables)) != len(path.tables):
        if bag is not None:
            bag.add(
                Diagnostic(
                    "SG501",
                    Severity.INFO,
                    "join snippet not rendered: the path revisits a table (a self-join), which "
                    "FactLang's render_join does not render",
                )
            )
        return None
    try:
        fl = importlib.import_module("factlang")
        sql = importlib.import_module("factlang.sql")
        refs = importlib.import_module("factlang.semantics.refs")
        edges = []
        for hop in path.hops:
            join = hop.edge.join
            left = tuple(_column(refs, c) for c in join.left)
            right = tuple(_column(refs, c) for c in join.right)
            if hop.reverse:
                left, right = right, left
            # JoinEdge carries key tuples and the cast (an EdgeRef holds one column pair only)
            edges.append(sql.JoinEdge(left, right, cast_to=join.cast_to))
        # FactLang keys aliases by table; the same alias per table as the rendered path text
        aliases = {
            _table(refs, ref): alias
            for ref, alias in zip(path.tables, path_aliases(list(path.tables)), strict=True)
        }
        filters: dict[Any, Any] = {}
        omitted: list[str] = []
        parse = getattr(fl, "parse_filter", None)
        for ref, text in sorted(soft_delete.items(), key=lambda kv: kv[0].canonical()):
            if ref not in path.tables:
                continue
            if not callable(parse):  # never silently: the agent must know live rows are unfiltered
                omitted.append(
                    f"-- live-rows filter for {qname(ref)} not applied "
                    f"(the installed FactLang lacks fl.parse_filter): {text}"
                )
                continue
            fl_table = _table(refs, ref)
            if catalog is not None:  # FactLang checks the columns; quoted names keep their case
                try:
                    cols = catalog.columns(fl_table) or {}
                except Exception:  # unknown table: FactLang reports what it could not check
                    cols = {}
                text, ambiguous = quote_catalog_names(text, cols)
                if ambiguous and bag is not None:
                    bag.add(
                        Diagnostic(
                            "SG506",
                            Severity.WARNING,
                            f"live-rows filter for {qname(ref)} names "
                            f"{', '.join(sorted(set(ambiguous)))}, which matches several catalog "
                            "columns differing only in case: left unquoted; quote it in the "
                            "overlay",
                        )
                    )
                expr, diags = parse(text, table=fl_table, catalog=catalog)
            else:
                expr, diags = parse(text, table=fl_table)
            if expr is not None and not [d for d in diags if _is_error(d)]:
                filters[_table(refs, ref)] = expr
            else:
                omitted.append(f"-- live-rows filter for {qname(ref)} unparseable, omitted")
        fragment = sql.render_join(edges, dialect, filters=filters or None, aliases=aliases)
        return "\n".join([str(fragment.inline()), *omitted])
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
