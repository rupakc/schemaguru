"""Read-only SQL guard helper (layer 6, spec 16.4). It judges text; it never executes.

This is a conservative DENY-LIST over text, NOT a sandbox. It is dialect-agnostic and refuses
whatever it cannot read the same way in every dialect: the SQL is lexed by a baseline lexer and
by each single dialect option (``#`` or ``//`` comments, ``--`` needing ASCII whitespace after
it, a lone CR ending a comment, ``$tag$`` strings, triple quotes, Oracle ``q'[...]'``, nested
``/* */``, backslash escapes, ``[ident]``), and every character must fall in the same class -
code, string-or-comment, or identifier - with every string, comment and quoted identifier
spanning exactly the same text. The one tolerated difference is a ``[...]`` region over baseline
code whose content has no quote, comment marker or ``;`` (array subscripts like ``x[1]`` and
``arr[OFFSET(0)]``). Anything else is refused as "ambiguous across SQL dialects"; since no single
option changes the lexing, no combination of options can either (see
:mod:`schemaguru._util.sqltext`). By design this refuses some valid dialect-specific SQL (``#``
comments, ``$$`` strings, ``x['key']``, triple quotes). ``U&`` escapes, backslashes in quoted
identifiers and ``/*!`` / ``/*M!`` executable comments are refused outright. Everything else is
checked once, on the agreed lexing: one statement, SELECT/WITH first, no DML/DDL keyword, no
function that runs SQL or opens a table or file named by text.

It does not stop side-effecting or resource-heavy read-only constructs (``pg_sleep``, expensive
scans): the host must still run queries under a read-only database role with limits.

``never_query`` is BEST-EFFORT, meant to keep a COOPERATIVE agent away from a table; it is not
isolation from an adversarial one, which needs warehouse permissions (a read-only role that
cannot see the table). It is enforced by name: any identifier outside a string or comment whose
text equals the banned table's last name part (case-insensitively, quoted or not) refuses the
SQL, wherever it appears and whatever catalog/schema qualifies it; so does any string literal
containing that name, after joining literals separated only by whitespace, comments or ``||``
(dynamic SQL is built from strings). Escape sequences inside literals are not decoded. Functions
that run SQL from text (``query_to_xml``, ``ts_stat``, DuckDB ``query``, ``dblink*``, ...) and
functions that read files or foreign databases (``pg_read_file``, ``lo_import``, ``read_csv*``,
``read_parquet*``, ``read_json*``, ``*_scan``, ...) are refused whatever their arguments, because
they reach data without naming it as a catalog table. The ``dialect`` argument is reserved.
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
        "schema_to_xml",
        "schema_to_xmlschema",
        "schema_to_xml_and_xmlschema",
        "database_to_xml",
        "database_to_xmlschema",
        "database_to_xml_and_xmlschema",
        "ts_stat",
        "ts_rewrite",
        "query",
        "pg_read_file",
        "pg_read_binary_file",
        "lo_import",
        "lo_export",
        "read_text",
        "read_blob",
        "exec",
        "execute",
        "sp_executesql",
        "openquery",
        "openrowset",
        "opendatasource",
    }
)
_TEXT_SQL_PREFIXES = ("dblink", "read_csv", "read_parquet", "read_json", "read_ndjson")
_LITERAL_GLUE = re.compile(r"(?:\s|\|\|)*")


def _reads_by_text(name: str) -> bool:
    return (
        name in _TEXT_SQL_FUNCTIONS or name.startswith(_TEXT_SQL_PREFIXES) or name.endswith("_scan")
    )


def _joined_literals(sql: str, kinds: str) -> list[str]:
    """String-literal contents, adjacent literals (only whitespace, comments, delimiters or ``||``
    between them) joined into one text."""
    out: list[str] = []
    prev_end = 0
    for m in _STRING_RUN.finditer(kinds):
        gap = "".join(
            c if k == "c" else " "
            for c, k in zip(sql[prev_end : m.start()], kinds[prev_end : m.start()], strict=True)
        )
        if out and _LITERAL_GLUE.fullmatch(gap):
            out[-1] += sql[m.start() : m.end()]
        else:
            out.append(sql[m.start() : m.end()])
        prev_end = m.end()
    return out


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
        add("SG801: '/*!' and '/*M!' executable comments are not allowed")
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
        if followed and _reads_by_text(folded):
            add(f"SG801: {folded}() runs SQL or reads a table or file named by text")
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
        for lit in _joined_literals(sql, lexed.kinds):
            lit = lit.casefold()
            for key, ref in banned.items():
                if key in lit:
                    add(f"SG802: a string literal names never-query table {ref.canonical()}")
    return GuardVerdict(not reasons, tuple(reasons))
