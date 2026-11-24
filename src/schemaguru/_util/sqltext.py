"""Conservative SQL text helpers (layer 0): quote/comment-aware lexing, no parsing.

Dialects disagree on comments (``--``, ``#``, ``//``, nested ``/* */``, MySQL's ``--`` needing a
following ASCII whitespace, a lone ``\\r`` ending a line comment), on quotes (``$tag$``, triple
quotes, Oracle ``q'[...]'``, ``[ident]``) and on backslash escapes. A lexer that guesses wrong can
be made to hide a statement inside a "literal" or inside a quoted identifier. Safety callers use
:func:`agreed_lex`: the baseline lexing is accepted only if :func:`agrees` holds for EVERY
single-option variant.

:func:`agrees` compares the per-character kinds exactly (code, string or comment content,
identifier content, and the delimiters of each), so every baseline string, comment and
identifier span is the identical span in the variant. The one tolerated difference is a
``[...]`` region of the bracket variant lying over baseline CODE whose content holds no quote
(``'``, ``"``, backtick), no comment marker (``--``, ``/*``, ``#``, ``//``) and no ``;``: such
content is plain code to every other option too, so treating it as code changes nothing.

Why single variants cover combinations: the lexer is a left-to-right sequence of decisions, each
made in code (which region starts here?) or inside a region (where does it end?). By induction,
if the baseline and a combination are in the same state at a position, they make the same
decision there: in code, an option of the combination could only act through its own trigger at
that position, but then the single variant for that option would already have differed (or,
for brackets, produced a harmless region that ends back in code with no trigger inside it); a
region's end depends on at most one option per region kind (backslash for quotes, CR for line
comments, nesting for block comments), each checked alone. So the combination's lexing equals
the baseline's. This holds only because the comparison is exact; a property test checks it on
random trigger-dense strings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
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
    cr: bool = False  # a lone '\r' ends a line comment
    bracket: bool = False  # [ident] quoting
    qquote: bool = False  # Oracle q'X...X' alternative quoting


# (variant, text that must occur for the variant to be able to differ from the baseline)
_VARIANTS: tuple[tuple[Opts, tuple[str, ...]], ...] = (
    (Opts(hash=True), ("#",)),
    (Opts(slash=True), ("//",)),
    (Opts(dd_space=True), ("--",)),
    (Opts(cr=True), ("\r",)),
    (Opts(dollar=True), ("$",)),
    (Opts(triple=True), ("'''", '"""')),
    (Opts(nested=True), ("/*",)),
    (Opts(backslash=True), ("\\",)),
    (Opts(bracket=True), ("[",)),
    (Opts(qquote=True), ("q'", "Q'")),
)

_ASCII_WS = frozenset(" \t\n\v\f\r")
_Q_CLOSE = {"[": "]", "(": ")", "{": "}", "<": ">"}

_DOLLAR = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")


class Lexed(NamedTuple):
    kinds: str  # per char: c code, q string content, d string delimiter, D dollar delimiter,
    #             i identifier content, e identifier delimiter, m comment,
    #             j bracket-identifier content, b bracket delimiter
    terminated: bool  # False when a literal or block comment is still open at the end
    bang: bool  # a '/*!' or '/*M!' executable comment was seen


@lru_cache(maxsize=64)
def _special(o: Opts) -> re.Pattern[str]:
    chars = set(o.quotes)
    if o.comments:
        chars |= {"-", "/"}
        if o.hash:
            chars.add("#")
    if o.dollar:
        chars.add("$")
    if o.bracket:
        chars.add("[")
    if o.qquote:
        chars |= {"q", "Q"}
    return re.compile("[" + re.escape("".join(sorted(chars))) + "]")


def _line_end(sql: str, i: int, cr: bool) -> int:
    n = len(sql)
    j = sql.find("\n", i)
    j = n if j < 0 else j
    if cr:
        k = sql.find("\r", i, j)
        while k >= 0 and sql[k + 1 : k + 2] == "\n":
            k = sql.find("\r", k + 1, j)
        if k >= 0:
            j = k
    return j


def _is_word(c: str) -> bool:
    return c.isalnum() or c in "_$"


def _word_before(sql: str, i: int) -> bool:
    """True when ``sql[i]`` continues a word (``nq'`` / ``Nq'`` national prefix excepted)."""
    if i == 0 or not _is_word(sql[i - 1]):
        return False
    return not (sql[i - 1] in "nN" and (i == 1 or not _is_word(sql[i - 2])))


def lex(sql: str, o: Opts) -> Lexed:
    out: list[str] = []
    n = len(sql)
    i = 0
    terminated = True
    bang = False
    special = _special(o)
    while i < n:
        m0 = special.search(sql, i)
        if m0 is None:
            out.append("c" * (n - i))
            break
        if m0.start() > i:
            out.append("c" * (m0.start() - i))
            i = m0.start()
        ch = sql[i]
        if o.comments and (
            (
                ch == "-"
                and sql.startswith("--", i)
                and (not o.dd_space or i + 2 >= n or sql[i + 2] in _ASCII_WS)
            )
            or (ch == "#" and o.hash)
            or (ch == "/" and o.slash and sql.startswith("//", i))
        ):
            j = _line_end(sql, i, o.cr)
            out.append("m" * (j - i))
            i = j
        elif o.comments and ch == "/" and sql.startswith("/*", i):
            if sql.startswith("!", i + 2) or sql[i + 2 : i + 4] in ("M!", "m!"):
                bang = True
            depth, j = 1, i + 2
            while j < n and depth:
                if o.nested:
                    k = min(
                        (x for x in (sql.find("/*", j), sql.find("*/", j)) if x >= 0), default=-1
                    )
                else:
                    k = sql.find("*/", j)
                if k < 0:
                    j = n
                    break
                if sql.startswith("/*", k) and o.nested:
                    depth += 1
                else:
                    depth -= 1
                j = k + 2
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
        elif ch == "[" and o.bracket:
            j = i + 1
            closed = False
            while True:
                k = sql.find("]", j)
                if k < 0:
                    break
                if sql.startswith("]]", k):
                    j = k + 2
                    continue
                closed = True
                j = k
                break
            if closed:
                out.append("b" + "j" * (j - i - 1) + "b")
                i = j + 1
            else:
                terminated = False
                out.append("b" + "j" * (n - i - 1))
                i = n
        elif ch in "qQ" and o.qquote:
            if (
                sql.startswith("'", i + 1)
                and i + 2 < n
                and sql[i + 2] not in " \t\r\n"
                and not _word_before(sql, i)
            ):
                close = _Q_CLOSE.get(sql[i + 2], sql[i + 2]) + "'"
                k = sql.find(close, i + 3)
                if k < 0:
                    terminated = False
                    out.append("ddd" + "q" * (n - i - 3))
                    i = n
                else:
                    out.append("ddd" + "q" * (k - i - 3) + "dd")
                    i = k + 2
            else:
                out.append("c")
                i += 1
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
            delim = "e" if kind == "i" else "d"
            j = i + 1
            closed = False
            if o.backslash:
                while j < n:
                    c = sql[j]
                    if c == "\\" and j + 1 < n:
                        j += 2
                    elif c == ch:
                        if j + 1 < n and sql[j + 1] == ch:
                            j += 2
                        else:
                            closed = True
                            break
                    else:
                        j += 1
            else:
                while True:
                    k = sql.find(ch, j)
                    if k < 0:
                        j = n
                        break
                    if sql.startswith(ch, k + 1):
                        j = k + 2
                        continue
                    closed = True
                    j = k
                    break
            j = min(j, n)
            out.append(delim + kind * (j - i - 1))
            if closed:
                out.append(delim)
                i = j + 1
            else:
                terminated = False
                i = n
        else:
            out.append("c")
            i += 1
    return Lexed("".join(out), terminated, bang)


_BRACKET_REGION = re.compile(r"bj*b?")
_BRACKET_UNSAFE = re.compile(r"[\'\"`;#]|--|/\*|//")
_BRACKET_AS_CODE = str.maketrans("bj", "cc")


def agrees(sql: str, base: Lexed, other: Lexed) -> bool:
    """Whether ``other`` lexes ``sql`` the same way as ``base`` (see the module docstring).

    Kinds must match exactly per character, except that a ``[...]`` region of ``other`` over
    baseline code counts as code when its content holds no quote, comment marker or ``;``.
    """
    if other.terminated != base.terminated or other.bang != base.bang:
        return False
    kinds = other.kinds
    if "b" in kinds:
        for m in _BRACKET_REGION.finditer(kinds):
            s, e = m.span()
            if base.kinds[s:e].count("c") != e - s:
                return False
            if _BRACKET_UNSAFE.search(sql, s + 1, e - 1 if kinds[e - 1 : e] == "b" else e):
                return False
        kinds = kinds.translate(_BRACKET_AS_CODE)
    return kinds == base.kinds


def agreed_lex(sql: str) -> tuple[Lexed, bool]:
    """Baseline lexing and whether every single-option variant :func:`agrees` with it.

    A variant whose trigger text does not occur in ``sql`` cannot differ, so it is skipped.
    """
    base = lex(sql, Opts())
    for o, triggers in _VARIANTS:
        if any(t in sql for t in triggers) and not agrees(sql, base, lex(sql, o)):
            return base, False
    return base, True


def scan(sql: str, o: Opts | None = None, *, backslash: bool = False) -> list[tuple[str, bool]]:
    """``(char, in_literal)`` per character; comments are not special (callers refuse them)."""
    opts = o if o is not None else Opts(quotes="'\"", comments=False, backslash=backslash)
    k = lex(sql, opts).kinds
    return [(c, t in "qidDejb") for c, t in zip(sql, k, strict=True)]


class Blanked(NamedTuple):
    view: str
    terminated: bool
    bang: bool


def blank(
    sql: str, o: Opts | None = None, *, keep_idents: bool = True, lexed: Lexed | None = None
) -> Blanked:
    """Same-length view: comments and string content become spaces (newlines kept) so offsets
    match ``sql``; ``"``/backtick identifier content is kept unless ``keep_idents`` is False."""
    lx = lexed if lexed is not None else lex(sql, o if o is not None else Opts())
    out: list[str] = []
    for c, t in zip(sql, lx.kinds, strict=True):
        if t in "cde" or (t in "ijb" and keep_idents):
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
