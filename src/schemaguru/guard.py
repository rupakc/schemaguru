"""Read-only SQL guard helper (layer 6, spec 16.4). It judges text; it never executes.

This is a conservative DENY-LIST over text, NOT a sandbox. It is dialect-agnostic and refuses
whatever it cannot read the same way in every dialect: the code/string/comment segmentation of
the SQL must be identical under the baseline lexer and under each single dialect option (``#`` or
``//`` comments, ``--`` needing a space, a lone CR ending a comment, ``$tag$`` strings, triple
quotes, nested ``/* */``, backslash escapes, ``[ident]``); otherwise the SQL is refused as
"ambiguous across SQL dialects". By design this refuses dialect-specific constructs whose meaning
differs across dialects (``#`` comments, ``$$`` strings, ``[ident]``, triple quotes), so some valid
SQL is refused. ``U&`` escapes, backslashes in quoted identifiers and ``/*!`` comments are
refused outright. Everything else is checked once, on the agreed segmentation: one statement,
SELECT/WITH first, no DML/DDL keyword, no function that runs SQL from text.

It does not stop side-effecting or resource-heavy read-only constructs (``pg_sleep``,
``pg_read_file``, expensive scans): the host must still run queries under a read-only database
role with limits.

``never_query`` is enforced by name: any identifier outside a string or comment whose text equals
the banned table's last name part (case-insensitively, quoted or not) refuses the SQL, wherever
it appears and whatever catalog/schema qualifies it; so does any string literal containing that
name (dynamic SQL is built from strings). The ``dialect`` argument is reserved.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass

from schemaguru._util.sqltext import agreed_lex, blank, tokenize
from schemaguru.model.refs import ObjectRef

MAX_SQL_CHARS = 100_000  # [call] generous for generated SQL, small enough to bound scanning
_ALLOWED_FIRST = frozenset({"SELECT", "WITH"})
_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|CREATE|ALTER|DROP|TRUNCATE|GRANT|REVOKE|COPY|CALL|EXECUTE"
    r"|ATTACH|DETACH|PRAGMA|VACUUM|INTO)\b",
    re.IGNORECASE,
)
_UNICODE_ESCAPE = re.compile(r"(?<![\w$])U&[\"']", re.IGNORECASE)
_STRING_RUN = re.compile(r"q+")
# functions that run SQL or open a table given as TEXT
_TEXT_SQL_FUNCTIONS = frozenset(
    {
        "query_to_xml",
        "query_to_xml_and_xmlschema",
        "query_to_xmlschema",
        "table_to_xml",
        "table_to_xml_and_xmlschema",
        "table_to_xmlschema",
        "cursor_to_xml",
        "query_table",
        "query_table_function",
        "dblink",
        "dblink_exec",
        "exec",
        "execute",
        "sp_executesql",
        "openquery",
        "openrowset",
        "opendatasource",
    }
)


@dataclass(frozen=True)
class GuardVerdict:
    ok: bool
    reasons: tuple[str, ...] = ()


def check_sql(
    sql: str, dialect: str | None = None, *, never_query: Collection[ObjectRef] = ()
) -> GuardVerdict:
    """Verdict on one statement of agent SQL (see the module docstring for the limits)."""
    if not isinstance(sql, str) or not sql.strip():
        return GuardVerdict(False, ("SG801: empty SQL",))
    if len(sql) > MAX_SQL_CHARS:
        return GuardVerdict(False, (f"SG801: longer than {MAX_SQL_CHARS} characters",))
    lexed, agreed = agreed_lex(sql)
    if not agreed:
        return GuardVerdict(False, ("SG801: ambiguous across SQL dialects",))
    reasons: list[str] = []

    def add(r: str) -> None:
        if r not in reasons:
            reasons.append(r)

    if lexed.bang:
        add("SG801: '/*!' conditional comments are not allowed")
    if not lexed.terminated:
        add("SG801: unterminated quote or comment")
    if any(c == "\\" and k == "i" for c, k in zip(sql, lexed.kinds, strict=True)):
        add("SG801: a backslash inside a quoted identifier is not allowed")
    code = blank(sql, keep_idents=False, lexed=lexed).view
    if _UNICODE_ESCAPE.search(code):
        add("SG801: U& escapes are not allowed")
    text = code.strip()
    if text.endswith(";"):
        text = text[:-1].rstrip()
    if ";" in text:
        add("SG801: more than one statement")
    bad = _FORBIDDEN.search(text)
    if bad:
        add(f"SG801: not read-only; found {bad.group(0).upper()}")
    first = text.lstrip("( \t\r\n").split(None, 1)[0].upper() if text.strip("( \t\r\n") else ""
    if first not in _ALLOWED_FIRST:
        add(f"SG801: must start with SELECT or WITH, got {first or 'nothing'!r}")

    banned = {b.table.casefold(): b.table_ref() for b in never_query}
    toks = tokenize(blank(sql, lexed=lexed).view)
    for i, t in enumerate(toks):
        if t.kind not in ("ident", "qident"):
            continue
        text_ = t.text.strip('"`')
        folded = text_.casefold()
        names = {folded} | {p.casefold() for p in text_.split(".")}
        for key, ref in banned.items():
            if key in names:
                add(f"SG802: reads never-query table {ref.canonical()}")
        followed = i + 1 < len(toks) and toks[i + 1].text == "("
        if followed and folded in _TEXT_SQL_FUNCTIONS:
            add(f"SG801: {folded}() runs SQL from text")
        if followed and folded in ("identifier", "table"):
            plain = (
                i + 3 < len(toks)
                and toks[i + 2].kind in ("ident", "qident")
                and toks[i + 3].text == ")"
            )
            if folded == "identifier" and not plain:
                add("SG801: IDENTIFIER() with a non-identifier argument cannot be checked")
            if folded == "table" and i + 2 < len(toks) and toks[i + 2].kind == "str":
                add("SG802: a string given to TABLE( cannot be checked")
    if banned:
        low = sql.casefold()
        for m in _STRING_RUN.finditer(lexed.kinds):
            lit = low[m.start() : m.end()]
            for key, ref in banned.items():
                if key in lit:
                    add(f"SG802: a string literal names never-query table {ref.canonical()}")
    return GuardVerdict(not reasons, tuple(reasons))
