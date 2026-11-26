"""In-process table and column lineage of one SQL definition (0.2.0 spec 4.1, 4.4).

``sqlglot`` (extra ``lineage``) is imported only inside functions. Names are resolved against the
store's known relations and columns only: anything the store does not hold goes to
``unresolved``, and a column that cannot be attributed is ``unknown`` -- nothing is guessed.

Privacy: a failure carries a fixed category (:data:`FailureReason`), never SQL text or a literal,
and the ``sqlglot`` logger (which quotes SQL) is disabled while a call runs.
"""

from __future__ import annotations

import importlib.util
import logging
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Any, Literal, TypeVar, cast

from schemaguru.lineage.dialects import sqlglot_dialect
from schemaguru.model.lineage import RawName, ResolvedTable, Transform
from schemaguru.model.refs import ObjectRef

FailureReason = Literal["parse_error", "too_large", "timeout", "unsupported_dialect", "worker_died"]

MAX_DEFINITION_CHARS = 1_000_000

_T = TypeVar("_T")


class LineageFailure(Exception):
    """A definition yielded no lineage; ``str(e)`` is the fixed :data:`FailureReason`."""

    def __init__(self, reason: FailureReason) -> None:
        super().__init__(reason)
        self.reason: FailureReason = reason

    def __str__(self) -> str:
        return self.reason


def lineage_available() -> bool:
    """True when the ``lineage`` extra (``sqlglot``) is installed."""
    return importlib.util.find_spec("sqlglot") is not None


@dataclass(frozen=True)
class ColumnLineage:
    column: str
    inputs: tuple[str, ...]
    transform: Transform
    unresolved: tuple[str, ...]


@dataclass(frozen=True)
class ParsedLineage:
    upstream: tuple[str, ...]
    unresolved: tuple[str, ...]
    columns: tuple[ColumnLineage, ...]


# ----- public entry points ---------------------------------------------------------------------


def extract_tables(sql: str, dialect: str) -> tuple[RawName, ...]:
    """The relations ``sql`` reads (CTE names and a recursive CTE's self reference excluded),
    sorted and unique. Raises only :class:`LineageFailure`."""
    return _guarded(lambda: _extract(sql, dialect))


def parse_lineage(
    sql: str,
    dialect: str,
    schema: Mapping[RawName, ResolvedTable],
    *,
    columns: bool = True,
) -> ParsedLineage:
    """Table lineage (``upstream``/``unresolved``) and, with ``columns``, projection lineage of
    each output column. Raises only :class:`LineageFailure`."""
    return _guarded(lambda: _parse(sql, dialect, schema, columns))


def _guarded(fn: Callable[[], _T]) -> _T:
    reason: FailureReason | None = None
    with _quiet_sqlglot():
        try:
            return fn()
        except LineageFailure as exc:
            reason = exc.reason
        except Exception:
            reason = "parse_error"
    # raised outside the ``except`` blocks: no chained exception can carry SQL text
    raise LineageFailure(reason)


_QUIET_LOCK = threading.Lock()
_quiet_depth = 0
_quiet_before = False


@contextmanager
def _quiet_sqlglot() -> Iterator[None]:
    """Disable the ``sqlglot`` logger while any call (in any thread) is parsing.

    ``Logger.disabled`` is process-global, so it is reference-counted: the first caller saves its
    state, the last one restores it; an inner exit never re-enables it under another thread.
    """
    global _quiet_depth, _quiet_before
    logger = logging.getLogger("sqlglot")
    with _QUIET_LOCK:
        if _quiet_depth == 0:
            _quiet_before = logger.disabled
            logger.disabled = True
        _quiet_depth += 1
    try:
        yield
    finally:
        with _QUIET_LOCK:
            _quiet_depth -= 1
            if _quiet_depth == 0:
                logger.disabled = _quiet_before


# ----- parsing and unwrapping ------------------------------------------------------------------


def _statement(sql: str, dialect: str) -> tuple[Any, tuple[str, ...] | None]:
    """The query of ``sql`` and, for a view with a column list, the output names."""
    if len(sql) > MAX_DEFINITION_CHARS:
        raise LineageFailure("too_large")
    read = sqlglot_dialect(dialect)
    if read is None or not lineage_available():
        raise LineageFailure("unsupported_dialect")
    import sqlglot
    from sqlglot import exp

    try:
        sqlglot.Dialect.get_or_raise(read)
    except Exception:
        raise LineageFailure("unsupported_dialect") from None
    stmts = [s for s in sqlglot.parse(sql, read=read) if s is not None]
    if len(stmts) != 1:
        raise LineageFailure("parse_error")
    node = stmts[0]
    names: tuple[str, ...] | None = None
    if isinstance(node, exp.Create):
        if str(node.args.get("kind") or "").upper() != "VIEW":
            raise LineageFailure("parse_error")
        target = node.this
        if isinstance(target, exp.Schema):
            names = tuple(e.name for e in target.expressions)
        node = node.expression
    if not isinstance(node, exp.Query):
        raise LineageFailure("parse_error")
    return node, names


def _arg(node: Any, *keys: str) -> Any:
    for k in keys:
        v = node.args.get(k)
        if v is not None:
            return v
    return None


def _with(node: Any) -> Any:
    return _arg(node, "with_", "with")


def _visible_ctes(with_node: Any, child: Any, dialect: str) -> list[str]:
    """CTE names of ``with_node`` visible from ``child`` (one of its CTEs, or the body)."""
    names = [c.alias.lower() for c in with_node.expressions]
    if child not in with_node.expressions:
        return names
    if with_node.args.get("recursive"):
        return names
    idx = next(i for i, c in enumerate(with_node.expressions) if c is child)
    # T-SQL has no RECURSIVE keyword: a CTE that names itself is recursive
    return names[: idx + 1] if dialect == "tsql" else names[:idx]


def _is_cte_ref(table: Any, dialect: str) -> bool:
    from sqlglot import exp

    if table.args.get("db") or table.args.get("catalog"):
        return False
    name = table.name.lower()
    child, node = table, table.parent
    while node is not None:
        if isinstance(node, exp.With):
            if name in _visible_ctes(node, child, dialect):
                return True
        else:
            w = _with(node)
            if (
                w is not None
                and w is not child
                and name in [c.alias.lower() for c in w.expressions]
            ):
                return True
        child, node = node, node.parent
    return False


def _raw_name(table: Any) -> RawName:
    return (table.catalog or None, table.db or None, table.name)


def _text(raw: RawName) -> str:
    return ".".join(p for p in raw if p)


def _relations(query: Any, dialect: str) -> list[Any]:
    from sqlglot import exp

    return [
        t
        for t in query.find_all(exp.Table)
        if isinstance(t.this, exp.Identifier) and not _is_cte_ref(t, dialect)
    ]


def _extract(sql: str, dialect: str) -> tuple[RawName, ...]:
    query, _ = _statement(sql, dialect)
    read = cast(str, sqlglot_dialect(dialect))
    raws = {_raw_name(t) for t in _relations(query, read)}
    return tuple(sorted(raws, key=lambda r: (r[0] or "", r[1] or "", r[2])))


def _parse(
    sql: str, dialect: str, schema: Mapping[RawName, ResolvedTable], columns: bool
) -> ParsedLineage:
    query, names = _statement(sql, dialect)
    read = cast(str, sqlglot_dialect(dialect))
    upstream: set[str] = set()
    unresolved: set[str] = set()
    for t in _relations(query, read):
        raw = _raw_name(t)
        hit = schema.get(raw)
        if hit is None:
            unresolved.add(_text(raw))
        else:
            upstream.add(hit.ref)
    cols: tuple[ColumnLineage, ...] = ()
    if columns:
        outs = _Walker(schema, read).query(query, None)
        cols = _finish(outs, names)
    return ParsedLineage(tuple(sorted(upstream)), tuple(sorted(unresolved)), cols)


# ----- column lineage --------------------------------------------------------------------------

_RANK = {"copy": 0, "cast": 1, "expression": 2, "window": 3, "aggregate": 4}


@dataclass(frozen=True)
class _Lin:
    inputs: frozenset[str] = frozenset()
    unresolved: frozenset[str] = frozenset()
    transform: str = "copy"


_UNKNOWN = _Lin(transform="unknown")


def _combine(outer: str, inner: list[_Lin]) -> _Lin:
    """``outer`` (the class of an expression) over the lineage of the columns it reads."""
    unresolved = frozenset().union(*(lin.unresolved for lin in inner))
    if outer == "unknown" or any(lin.transform == "unknown" for lin in inner):
        return _Lin(frozenset(), unresolved, "unknown")
    transform = max([outer, *(lin.transform for lin in inner)], key=_RANK.__getitem__)
    return _Lin(frozenset().union(*(lin.inputs for lin in inner)), unresolved, transform)


def _merge(lins: list[_Lin]) -> _Lin:
    """Lineage of one output position fed by several branches (set operations, duplicates)."""
    return _combine("copy", lins)


@dataclass
class _Outs:
    """The output columns of a query; ``open`` when some are missing (a star over a relation
    whose columns are not known)."""

    cols: list[tuple[str | None, _Lin]]
    open: bool = False

    def renamed(self, names: list[str]) -> _Outs:
        if not names:
            return self
        cols: list[tuple[str | None, _Lin]] = [
            (n, lin) for n, (_, lin) in zip(names, self.cols, strict=False)
        ]
        cols += [(n, _UNKNOWN) for n in names[len(cols) :]]
        return _Outs(cols, self.open)


@dataclass
class _Source:
    kind: Literal["table", "unresolved", "derived", "unnest", "opaque", "self"]
    alias: str
    ref: str = ""
    columns: tuple[str, ...] = ()
    outs: _Outs | None = None
    lin: _Lin = _UNKNOWN
    names: tuple[str, ...] = ()

    @property
    def open(self) -> bool:
        if self.kind in ("unresolved", "opaque", "self"):
            return True
        if self.kind == "unnest":
            return not self.names
        return self.kind == "derived" and self.outs is not None and self.outs.open

    def lookup(self, col: str) -> _Lin | None:
        """Lineage of ``col`` read from this source; ``None`` when it does not have ``col``."""
        if self.kind == "table":
            name = _match(col, self.columns)
            if name is None:
                return None
            return _Lin(frozenset({_column_ref(self.ref, name)}))
        if self.kind == "unresolved":
            return _Lin(unresolved=frozenset({f"{self.ref}.{col}"}))
        if self.kind == "self":
            return _Lin()
        if self.kind == "opaque":
            return _UNKNOWN
        if self.kind == "unnest":
            if not self.names or _match(col, self.names) is not None:
                return self.lin
            return None
        assert self.outs is not None
        named = [n for n, _ in self.outs.cols if n is not None]
        hit = _match(col, tuple(named))
        if hit is None:
            return _UNKNOWN if self.outs.open else None
        return _merge([lin for n, lin in self.outs.cols if n == hit])

    def star(self) -> _Outs:
        if self.kind == "table":
            return _Outs([(c, cast(_Lin, self.lookup(c))) for c in self.columns])
        if self.kind == "derived" and self.outs is not None:
            return _Outs(list(self.outs.cols), self.outs.open)
        if self.kind == "unnest" and self.names:
            return _Outs([(n, self.lin) for n in self.names])
        return _Outs([], True)

    def missing(self, col: str) -> _Lin:
        """Lineage of a column this source was named for but does not hold."""
        if self.kind == "table":
            return _Lin(unresolved=frozenset({_column_ref(self.ref, col)}))
        return _UNKNOWN


def _column_ref(table_ref: str, column: str) -> str:
    return replace(ObjectRef.parse(table_ref), column=column).canonical()


def _match(name: str, names: tuple[str, ...]) -> str | None:
    """``name`` among ``names``: exact, else the unique case-insensitive match."""
    if name in names:
        return name
    folded = [n for n in names if n.lower() == name.lower()]
    return folded[0] if len(folded) == 1 else None


@dataclass
class _Env:
    parent: _Env | None
    ctes: dict[str, _Outs | None] = field(default_factory=dict)  # None: recursive self
    sources: list[_Source] = field(default_factory=list)
    aliases: dict[str, _Lin] = field(default_factory=dict)

    def derived_scope(self) -> _Env:
        """The scope a non-lateral derived table sees: this query's CTEs, not its siblings."""
        return _Env(self.parent, ctes=self.ctes)

    def cte(self, name: str) -> tuple[bool, _Outs | None]:
        env: _Env | None = self
        while env is not None:
            if name in env.ctes:
                return True, env.ctes[name]
            env = env.parent
        return False, None

    def source(self, alias: str) -> _Source | None:
        env: _Env | None = self
        while env is not None:
            names = tuple(s.alias for s in env.sources)
            hit = _match(alias, names)
            if hit is not None:
                return next(s for s in env.sources if s.alias == hit)
            env = env.parent
        return None


class _Walker:
    def __init__(self, schema: Mapping[RawName, ResolvedTable], dialect: str) -> None:
        from sqlglot import exp

        self.exp = exp
        self.schema = schema
        self.dialect = dialect

    # -- queries --

    def query(self, node: Any, parent: _Env | None) -> _Outs:
        exp = self.exp
        if isinstance(node, exp.Subquery):
            return self.query(node.this, parent)
        env = _Env(parent)
        self.bind_ctes(node, env)
        if isinstance(node, exp.SetOperation):
            return self.set_op(node, env)
        if isinstance(node, exp.Select):
            return self.select(node, env)
        if isinstance(node, exp.Values):
            width = len(node.expressions[0].expressions) if node.expressions else 0
            return _Outs([(None, _Lin(transform="expression"))] * width)
        return _Outs([], True)

    def set_op(self, node: Any, env: _Env) -> _Outs:
        left = self.query(node.this, env)
        right = self.query(node.expression, env)
        if left.open or right.open or len(left.cols) != len(right.cols):
            return _Outs([(n, _UNKNOWN) for n, _ in left.cols], True)
        return _Outs(
            [(n, _merge([a, b])) for (n, a), (_, b) in zip(left.cols, right.cols, strict=False)]
        )

    def bind_ctes(self, node: Any, env: _Env) -> None:
        w = _with(node)
        if w is None:
            return
        recursive = bool(w.args.get("recursive")) or self.dialect == "tsql"
        for cte in w.expressions:
            name = cte.alias.lower()
            names = [c.name for c in (cte.args["alias"].columns or [])]
            body = cte.this
            if recursive and self.reads(body, name):
                env.ctes[name] = None  # the self reference contributes nothing
                outs = self.query(body, env)
            else:
                outs = self.query(body, env)
            env.ctes[name] = outs.renamed(names)

    def reads(self, body: Any, name: str) -> bool:
        return any(
            not (t.args.get("db") or t.args.get("catalog")) and t.name.lower() == name
            for t in body.find_all(self.exp.Table)
        )

    def select(self, node: Any, env: _Env) -> _Outs:
        exp = self.exp
        from_ = _arg(node, "from_", "from")
        if from_ is not None:
            env.sources.extend(self.chain(from_.this, env))
        for join in node.args.get("joins") or []:
            env.sources.extend(self.join_sources(join, env))
        out = _Outs([])
        for proj in node.expressions:
            if isinstance(proj, exp.Star) or (
                isinstance(proj, exp.Column) and isinstance(proj.this, exp.Star)
            ):
                part = self.star(proj, env)
                out.cols.extend(part.cols)
                out.open = out.open or part.open
                continue
            name = proj.alias_or_name or None
            lin = self.expr(proj.this if isinstance(proj, exp.Alias) else proj, env)
            out.cols.append((name, lin))
            if name is not None:
                env.aliases.setdefault(name.lower(), lin)
        return out

    def star(self, proj: Any, env: _Env) -> _Outs:
        exp = self.exp
        if isinstance(proj, exp.Column):
            src = env.source(proj.table)
            part = src.star() if src is not None else _Outs([], True)
            star = proj.this
        else:
            part, star = _Outs([]), proj
            for s in env.sources:
                one = s.star()
                part.cols.extend(one.cols)
                part.open = part.open or one.open
        drop = {c.name.lower() for c in (_arg(star, "except_", "except") or [])}
        repl = {
            r.alias.lower(): self.expr(r.this, env)
            for r in (star.args.get("replace") or [])
            if isinstance(r, exp.Alias)
        }
        cols = [
            (n, repl.get(n.lower(), lin) if n else lin)
            for n, lin in part.cols
            if n is None or n.lower() not in drop
        ]
        return _Outs(cols, part.open)

    # -- sources --

    def join_sources(self, join: Any, env: _Env) -> list[_Source]:
        exp = self.exp
        this = join.this
        if str(join.args.get("kind") or "").upper() == "ARRAY":  # ClickHouse ARRAY JOIN
            target = this.this if isinstance(this, exp.Alias) else this
            name = this.alias_or_name
            return [_Source("unnest", name, lin=self.unnest_lin(target, env), names=(name,))]
        return self.chain(this, env)

    def chain(self, node: Any, env: _Env) -> list[_Source]:
        """The sources a FROM element contributes. An unaliased parenthesised join
        (``(a JOIN b ON …)``, DuckDB's stored-view form) parses as a ``Subquery`` around the first
        table, with the joins hung on the tables themselves; it registers its tables exactly as
        an unparenthesised join does. An aliased group, or a parenthesised query, is one source."""
        exp = self.exp
        out: list[_Source]
        if (
            isinstance(node, exp.Subquery)
            and node.args.get("alias") is None
            and isinstance(node.this, exp.Table | exp.Subquery)
        ):
            out = self.chain(node.this, env)
        else:
            out = [self.source(node, env)]
        for join in node.args.get("joins") or []:
            out.extend(self.join_sources(join, env))
        return out

    def source(self, node: Any, env: _Env) -> _Source:
        exp = self.exp
        alias_node = node.args.get("alias")
        alias = node.alias_or_name
        renames = [c.name for c in (alias_node.columns or [])] if alias_node is not None else []
        if isinstance(node, exp.Table) and isinstance(node.this, exp.Identifier):
            src = self.table(node, env, node.alias_or_name)
        elif isinstance(node, exp.Subquery):
            src = _Source("derived", alias, outs=self.query(node.this, env.derived_scope()))
        elif isinstance(node, exp.Lateral) and isinstance(node.this, exp.Subquery):
            src = _Source("derived", alias, outs=self.query(node.this.this, env))
        elif isinstance(node, exp.Lateral | exp.Unnest):
            target = node.this if isinstance(node, exp.Lateral) else node
            src = _Source("unnest", alias, lin=self.unnest_lin(target, env), names=tuple(renames))
            if isinstance(node, exp.Unnest) and alias_node is not None and not alias_node.name:
                src.alias = ""  # BigQuery ``UNNEST(x) AS v``: v is a column, not a relation
            return src
        elif isinstance(node, exp.Values):
            src = _Source("derived", alias, outs=self.query(node, env.derived_scope()))
        else:
            return _Source("opaque", alias)
        if renames:
            if src.kind == "table":
                src = _Source("derived", src.alias, outs=src.star())
            if src.kind == "derived" and src.outs is not None:
                src.outs = src.outs.renamed(renames)
        return src

    def table(self, node: Any, env: _Env, alias: str) -> _Source:
        raw = _raw_name(node)
        if raw[0] is None and raw[1] is None:
            found, outs = env.cte(raw[2].lower())
            if found:
                if outs is None:
                    return _Source("self", alias)
                return _Source("derived", alias, outs=outs)
        hit = self.schema.get(raw)
        if hit is None:
            return _Source("unresolved", alias, ref=_text(raw))
        return _Source("table", alias, ref=hit.ref, columns=hit.columns)

    def unnest_lin(self, target: Any, env: _Env) -> _Lin:
        lins = [self.column(c, env) for c in self.columns_in(target)]
        lins += [self.first(q, env) for q in self.queries_in(target)]
        return _combine("expression", lins)

    # -- expressions --

    def expr(self, node: Any, env: _Env) -> _Lin:
        if isinstance(node, self.exp.Query | self.exp.Subquery):  # a scalar subquery
            return _combine("expression", [self.first(node, env)])
        lins = [self.column(c, env) for c in self.columns_in(node)]
        lins += [self.first(q, env) for q in self.queries_in(node)]
        return _combine(self.classify(node), lins)

    def first(self, query: Any, env: _Env) -> _Lin:
        outs = self.query(query, env)
        return outs.cols[0][1] if outs.cols and not outs.open else _UNKNOWN

    def walk(self, node: Any) -> Iterator[Any]:
        """Nodes of ``node`` outside nested queries (whose roots are yielded, not entered)."""
        exp = self.exp
        stack = [node]
        while stack:
            cur = stack.pop()
            yield cur
            if cur is not node and isinstance(cur, exp.Query | exp.Subquery):
                continue
            for child in cur.iter_expressions():
                stack.append(child)

    def columns_in(self, node: Any) -> list[Any]:
        exp = self.exp
        return [
            n
            for n in self.walk(node)
            if isinstance(n, exp.Column) and not isinstance(n.this, exp.Star)
        ]

    def queries_in(self, node: Any) -> list[Any]:
        exp = self.exp
        return [
            n for n in self.walk(node) if n is not node and isinstance(n, exp.Query | exp.Subquery)
        ]

    def classify(self, node: Any) -> str:
        exp = self.exp
        while isinstance(node, exp.Paren):
            node = node.this
        if isinstance(node, exp.Column):
            return "copy"
        inner, casts = node, 0
        while isinstance(inner, exp.Cast | exp.Convert | exp.Paren):
            casts += not isinstance(inner, exp.Paren)
            inner = inner.expression if isinstance(inner, exp.Convert) else inner.this
        if casts and isinstance(inner, exp.Column):
            return "cast"
        windowed: set[int] = set()
        aggregate = window = False
        for n in self.walk(node):
            if isinstance(n, exp.Window):
                window = True
                cur = n.this
                while cur is not None and not isinstance(cur, exp.AggFunc) and "this" in cur.args:
                    cur = cur.this if isinstance(cur, exp.Expression) else None
                if isinstance(cur, exp.AggFunc):
                    windowed.add(id(cur))
            elif isinstance(n, exp.AggFunc) and id(n) not in windowed:
                aggregate = True
        if aggregate:
            return "aggregate"
        return "window" if window else "expression"

    def column(self, col: Any, env: _Env) -> _Lin:
        name = col.name
        if col.table:
            src = env.source(col.table)
            if src is None:
                return _UNKNOWN
            hit = src.lookup(name)
            return src.missing(name) if hit is None else hit
        e: _Env | None = env
        while e is not None:
            found = self.unqualified(name, e)
            if found is not None:
                return found
            e = e.parent
        if len(env.sources) == 1:  # one relation, named for a column it does not hold
            return env.sources[0].missing(name)
        return _UNKNOWN

    def unqualified(self, name: str, env: _Env) -> _Lin | None:
        """``name`` read without a qualifier in ``env``'s own scope; ``None``: look outward."""
        for src in env.sources:
            if src.kind == "unnest" and not src.alias and _match(name, src.names) is not None:
                return src.lin
        closed = [s for s in env.sources if not s.open and s.lookup(name) is not None]
        open_ = [s for s in env.sources if s.open]
        if len(closed) + len(open_) > 1:
            return _UNKNOWN
        if closed:
            return cast(_Lin, closed[0].lookup(name))
        alias = env.aliases.get(name.lower())
        if open_:
            # the open relation may hold ``name`` itself: never prefer a select-list alias
            return _UNKNOWN if alias is not None else cast(_Lin, open_[0].lookup(name))
        return alias


def _finish(outs: _Outs, names: tuple[str, ...] | None) -> tuple[ColumnLineage, ...]:
    cols = outs.renamed(list(names)).cols if names else outs.cols
    grouped: dict[str, list[_Lin]] = {}
    for name, lin in cols:
        if name:
            grouped.setdefault(name, []).append(lin)
    result = []
    for name in sorted(grouped):
        lin = _merge(grouped[name])
        transform = cast(Transform, lin.transform)
        inputs = () if transform == "unknown" else tuple(sorted(lin.inputs))
        result.append(ColumnLineage(name, inputs, transform, tuple(sorted(lin.unresolved))))
    return tuple(result)
