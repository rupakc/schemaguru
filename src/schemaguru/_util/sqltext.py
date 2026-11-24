"""Conservative SQL text helpers (layer 0): quote/comment-aware scanning, no parsing.

Used by the metadata executor and the runtime guard. They judge text that may be untrusted, so
callers that make a safety decision must examine BOTH backslash interpretations (``backslash``
False and True): dialects disagree on whether ``\\'`` escapes a quote, and a scanner that guesses
wrong can be made to hide a statement inside a "literal".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

DEFAULT_QUOTES = "'\"`"


def kinds(
    sql: str,
    quotes: str = "'\"",
    *,
    comments: bool = False,
    backslash: bool = False,
) -> tuple[str, bool]:
    """Classify every character: ``c`` code, ``q`` literal content, ``d`` quote delimiter,
    ``m`` comment. Returns ``(kinds, terminated)``; ``terminated`` is False when a literal or
    block comment is still open at the end. A doubled quote inside a literal is content;
    with ``backslash`` a backslash also escapes the next character inside a literal.
    """
    out: list[str] = []
    n = len(sql)
    i = 0
    quote: str | None = None
    terminated = True
    while i < n:
        ch = sql[i]
        if quote is not None:
            if backslash and ch == "\\" and i + 1 < n:
                out.append("q")
                out.append("q")
                i += 2
                continue
            if ch == quote:
                if i + 1 < n and sql[i + 1] == quote:
                    out.append("q")
                    out.append("q")
                    i += 2
                    continue
                out.append("d")
                quote = None
            else:
                out.append("q")
            i += 1
        elif comments and ch == "-" and sql.startswith("--", i):
            j = sql.find("\n", i)
            j = n if j < 0 else j
            out.extend("m" * (j - i))
            i = j
        elif comments and ch == "/" and sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            if j < 0:
                out.extend("m" * (n - i))
                terminated = False
                i = n
            else:
                out.extend("m" * (j + 2 - i))
                i = j + 2
        elif ch in quotes:
            quote = ch
            out.append("d")
            i += 1
        else:
            out.append("c")
            i += 1
    if quote is not None:
        terminated = False
    return "".join(out), terminated


def scan(sql: str, *, backslash: bool = False) -> list[tuple[str, bool]]:
    """``(char, in_literal)`` per character so rewriting never touches quoted text."""
    k, _ = kinds(sql, "'\"", backslash=backslash)
    return [(c, t in "qd") for c, t in zip(sql, k, strict=True)]


def has_backslash_in_literal(sql: str, quotes: str = DEFAULT_QUOTES) -> bool:
    k, _ = kinds(sql, quotes, comments=True)
    return any(c == "\\" and t == "q" for c, t in zip(sql, k, strict=True))


def blank(
    sql: str,
    *,
    quotes: str = DEFAULT_QUOTES,
    blank_quotes: str = DEFAULT_QUOTES,
    backslash: bool = False,
) -> tuple[str, bool]:
    """Same-length view: comments and the content of ``blank_quotes`` literals become spaces
    (newlines kept) so offsets still match ``sql``. Returns ``(view, terminated)``."""
    k, terminated = kinds(sql, quotes, comments=True, backslash=backslash)
    out: list[str] = []
    open_quote = ""
    for c, t in zip(sql, k, strict=True):
        if t == "m":
            out.append("\n" if c == "\n" else " ")
        elif t == "d":
            open_quote = c if not open_quote else ""
            out.append(c)
        elif t == "q" and open_quote in blank_quotes and open_quote:
            out.append("\n" if c == "\n" else " ")
        else:
            out.append(c)
    return "".join(out), terminated


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
    """Tokens of a :func:`blank` view (literals already blanked, comments gone)."""
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
    [
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
    ]
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


def table_uses(sql: str, *, backslash: bool = False) -> list[TableUse]:
    """Tables named after FROM / JOIN / comma-joins, CTE names and sub-selects excluded."""
    view, _ = blank(sql, blank_quotes="'", backslash=backslash)
    toks = tokenize(view)
    ctes = {
        toks[i].text.casefold()
        for i in range(len(toks) - 2)
        if toks[i].kind in ("ident", "qident")
        and toks[i + 1].text.upper() == "AS"
        and toks[i + 2].text == "("
    }
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
