"""Conservative SQL text helpers (layer 0): quote/comment-aware lexing, no parsing.

Dialects disagree on comments (``--``, ``#``, ``//``, nested ``/* */``, MySQL's ``--`` needing a
following space), on quotes (``$tag$``, triple quotes) and on backslash escapes. A lexer that
guesses wrong can be made to hide a statement inside a "literal". Callers that make a safety
decision therefore lex once per variant in :data:`GUARD_VARIANTS` and refuse if ANY variant shows
a violation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import NamedTuple


@dataclass(frozen=True)
class Opts:
    quotes: str = "'\"`"
    comments: bool = True
    hash: bool = False  # '#' line comment
    slash: bool = False  # '//' line comment
    dollar: bool = False  # $tag$ ... $tag$ strings
    triple: bool = False  # ''' ... ''' and """ ... """
    nested: bool = False  # nesting /* /* */ */
    dd_space: bool = False  # '--' is a comment only when followed by whitespace
    backslash: bool = False  # backslash escapes the next character inside a literal


_ALL = Opts(hash=True, slash=True, dollar=True, triple=True, nested=True, backslash=True)
GUARD_VARIANTS: tuple[Opts, ...] = (
    Opts(),
    Opts(hash=True),
    Opts(slash=True),
    Opts(dollar=True),
    Opts(triple=True),
    Opts(nested=True),
    Opts(dd_space=True),
    Opts(backslash=True),
    _ALL,
    replace(_ALL, dd_space=True),
)

_DOLLAR = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")


class Lexed(NamedTuple):
    kinds: str  # per char: c code, q string content, i identifier content, d delimiter,
    #             D dollar delimiter, m comment
    terminated: bool  # False when a literal or block comment is still open at the end
    bang: bool  # a '/*!' conditional comment was seen


def lex(sql: str, o: Opts) -> Lexed:
    out: list[str] = []
    n = len(sql)
    i = 0
    terminated = True
    bang = False
    while i < n:
        ch = sql[i]
        if o.comments and (
            (
                ch == "-"
                and sql.startswith("--", i)
                and (not o.dd_space or i + 2 >= n or sql[i + 2].isspace() or ord(sql[i + 2]) < 32)
            )
            or (ch == "#" and o.hash)
            or (ch == "/" and o.slash and sql.startswith("//", i))
        ):
            j = sql.find("\n", i)
            j = n if j < 0 else j
            out.append("m" * (j - i))
            i = j
        elif o.comments and ch == "/" and sql.startswith("/*", i):
            if sql.startswith("!", i + 2):
                bang = True
            depth, j = 1, i + 2
            while j < n and depth:
                if o.nested and sql.startswith("/*", j):
                    depth += 1
                    j += 2
                elif sql.startswith("*/", j):
                    depth -= 1
                    j += 2
                else:
                    j += 1
            if depth:
                terminated = False
            out.append("m" * (j - i))
            i = j
        elif (
            ch == "$"
            and o.dollar
            and (i == 0 or not (sql[i - 1].isalnum() or sql[i - 1] in "_$"))
            and (m := _DOLLAR.match(sql, i))
        ):
            tag = m.group()
            k = sql.find(tag, m.end())
            if k < 0:
                terminated = False
                out.append("D" * len(tag) + "q" * (n - m.end()))
                i = n
            else:
                out.append("D" * len(tag) + "q" * (k - m.end()) + "D" * len(tag))
                i = k + len(tag)
        elif ch in o.quotes:
            if o.triple and ch in "'\"" and sql.startswith(ch * 3, i):
                j = i + 3
                while j < n and not sql.startswith(ch * 3, j):
                    j += 2 if (o.backslash and sql[j] == "\\") else 1
                j = min(j, n)
                if j >= n:
                    terminated = False
                    out.append("ddd" + "q" * (n - i - 3))
                    i = n
                else:
                    out.append("ddd" + "q" * (j - i - 3) + "ddd")
                    i = j + 3
                continue
            kind = "i" if ch in '"`' else "q"
            j = i + 1
            closed = False
            while j < n:
                c = sql[j]
                if o.backslash and c == "\\" and j + 1 < n:
                    j += 2
                elif c == ch:
                    if j + 1 < n and sql[j + 1] == ch:
                        j += 2
                    else:
                        closed = True
                        break
                else:
                    j += 1
            j = min(j, n)
            out.append("d" + kind * (j - i - 1))
            if closed:
                out.append("d")
                i = j + 1
            else:
                terminated = False
                i = n
        else:
            out.append("c")
            i += 1
    return Lexed("".join(out), terminated, bang)


def scan(sql: str, o: Opts | None = None, *, backslash: bool = False) -> list[tuple[str, bool]]:
    """``(char, in_literal)`` per character; comments are not special (callers refuse them)."""
    opts = o if o is not None else Opts(quotes="'\"", comments=False, backslash=backslash)
    k = lex(sql, opts).kinds
    return [(c, t in "qidD") for c, t in zip(sql, k, strict=True)]


class Blanked(NamedTuple):
    view: str
    terminated: bool
    bang: bool


def blank(sql: str, o: Opts | None = None, *, keep_idents: bool = True) -> Blanked:
    """Same-length view: comments and string content become spaces (newlines kept) so offsets
    match ``sql``; ``"``/backtick identifier content is kept unless ``keep_idents`` is False."""
    lx = lex(sql, o if o is not None else Opts())
    out: list[str] = []
    for c, t in zip(sql, lx.kinds, strict=True):
        if t == "c" or t == "d" or (t == "i" and keep_idents):
            out.append(c)
        elif t == "D":
            out.append("'")
        else:
            out.append("\n" if c == "\n" else " ")
    return Blanked("".join(out), lx.terminated, lx.bang)


@dataclass(frozen=True)
class Tok:
    kind: str  # ident | qident | str | num | punct
    text: str
    start: int
    end: int


_TOKEN = re.compile(
    r"""(?P<qident>"[^"]*"|`[^`]*`)|(?P<str>'[^']*')|(?P<num>\d+(?:\.\d+)?)"""
    r"""|(?P<ident>[A-Za-z_][A-Za-z0-9_$]*)|(?P<punct>[^\s])"""
)


def tokenize(view: str) -> list[Tok]:
    """Tokens of a :func:`blank` view (strings already blanked, comments gone)."""
    return [
        Tok(m.lastgroup or "punct", m.group(), m.start(), m.end()) for m in _TOKEN.finditer(view)
    ]


NamePart = tuple[str, bool]  # (text, double-quoted)


@dataclass(frozen=True)
class TableUse:
    parts: tuple[NamePart, ...]
    alias: str | None
    start: int


_NOT_ALIAS = frozenset(
    {
        "WHERE",
        "ON",
        "JOIN",
        "LEFT",
        "RIGHT",
        "INNER",
        "OUTER",
        "FULL",
        "CROSS",
        "NATURAL",
        "GROUP",
        "ORDER",
        "LIMIT",
        "UNION",
        "INTERSECT",
        "EXCEPT",
        "HAVING",
        "USING",
        "WINDOW",
        "QUALIFY",
        "OFFSET",
        "FETCH",
        "FOR",
        "SET",
        "SELECT",
        "LATERAL",
        "TABLESAMPLE",
    }
)


def _name_part(t: Tok) -> list[NamePart] | None:
    if t.kind == "ident":
        return [(t.text, False)]
    if t.kind == "qident":
        inner = t.text[1:-1]
        if t.text[0] == "`":
            return [(p, False) for p in inner.split(".") if p]
        return [(inner, True)]
    return None


def _skip_parens(toks: list[Tok], k: int) -> int:
    """Index after the ')' matching the '(' at ``k`` (``len(toks)`` if unbalanced)."""
    depth = 0
    while k < len(toks):
        if toks[k].text == "(":
            depth += 1
        elif toks[k].text == ")":
            depth -= 1
            if depth == 0:
                return k + 1
        k += 1
    return k


def _cte_names(toks: list[Tok]) -> set[str]:
    """Names declared by a ``WITH [RECURSIVE] name [(cols)] AS [MATERIALIZED] (...) , ...`` list."""
    names: set[str] = set()
    n = len(toks)
    for w, t in enumerate(toks):
        if t.kind != "ident" or t.text.upper() != "WITH":
            continue
        j = w + 1
        if j < n and toks[j].text.upper() == "RECURSIVE":
            j += 1
        while j < n and toks[j].kind in ("ident", "qident"):
            name = toks[j].text.strip('"`').casefold()
            k = j + 1
            if k < n and toks[k].text == "(":
                k = _skip_parens(toks, k)
            if k >= n or toks[k].text.upper() != "AS":
                break
            k += 1
            while k < n and toks[k].text.upper() in ("NOT", "MATERIALIZED"):
                k += 1
            if k >= n or toks[k].text != "(":
                break
            names.add(name)
            k = _skip_parens(toks, k)
            if k < n and toks[k].text == ",":
                j = k + 1
                continue
            break
    return names


def table_uses(sql: str, o: Opts | None = None) -> list[TableUse]:
    """Tables named after FROM / JOIN / comma-joins (best effort: NOT a safety boundary)."""
    toks = tokenize(blank(sql, o).view)
    ctes = _cte_names(toks)
    uses: list[TableUse] = []
    n = len(toks)
    i = 0
    while i < n:
        word = toks[i].text.upper() if toks[i].kind == "ident" else ""
        if word not in ("FROM", "JOIN"):
            i += 1
            continue
        i += 1
        while True:
            parts: list[NamePart] = []
            start = toks[i].start if i < n else 0
            while i < n:
                got = _name_part(toks[i])
                if got is None:
                    break
                parts.extend(got)
                i += 1
                if i + 1 < n and toks[i].text == "." and _name_part(toks[i + 1]) is not None:
                    i += 1
                    continue
                break
            if not parts or (i < n and toks[i].text == "("):
                break  # sub-select or table function
            alias: str | None = None
            if i < n and toks[i].kind == "ident" and toks[i].text.upper() == "AS":
                i += 1
            if (
                i < n
                and toks[i].kind in ("ident", "qident")
                and toks[i].text.upper() not in _NOT_ALIAS
            ):
                alias = toks[i].text.strip('"`')
                i += 1
            single = len(parts) == 1 and parts[0][0].casefold() in ctes
            if not single:
                uses.append(TableUse(tuple(parts), alias, start))
            if word == "FROM" and i < n and toks[i].text == ",":
                i += 1
                continue
            break
    return uses
