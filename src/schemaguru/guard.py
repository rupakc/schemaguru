"""Read-only SQL guard helper (layer 6, spec 16.4). It judges text; it never executes.

Stricter than the metadata guard: one statement, SELECT/WITH only, a length cap, a DML/DDL
blocklist applied after comments and literals are blanked, and ``never_query`` tables refused.
The input is untrusted, so the text is judged under BOTH backslash interpretations (dialects
disagree on whether ``\\'`` escapes a quote); a violation under either refuses the SQL.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass

from schemaguru._util.sqltext import TableUse, blank, table_uses
from schemaguru.model.refs import ObjectRef

MAX_SQL_CHARS = 100_000  # [call] generous for generated SQL, small enough to bound scanning
_ALLOWED_FIRST = frozenset({"SELECT", "WITH"})
_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|CREATE|ALTER|DROP|TRUNCATE|GRANT|REVOKE|COPY|CALL|EXECUTE"
    r"|ATTACH|DETACH|PRAGMA|VACUUM|INTO)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class GuardVerdict:
    ok: bool
    reasons: tuple[str, ...] = ()


def _not_read_only(sql: str, backslash: bool) -> list[str]:
    view, terminated = blank(sql, backslash=backslash)
    if not terminated:
        return ["SG801: unterminated quote or comment (or an ambiguous backslash escape)"]
    code = view.strip()
    if code.endswith(";"):
        code = code[:-1].rstrip()
    out: list[str] = []
    if ";" in code:
        out.append("SG801: more than one statement")
    bad = _FORBIDDEN.search(code)
    if bad:
        out.append(f"SG801: not read-only; found {bad.group(0).upper()}")
    first = code.lstrip("( \t\r\n").split(None, 1)[0].upper() if code.strip("( \t\r\n") else ""
    if first not in _ALLOWED_FIRST:
        out.append(f"SG801: must start with SELECT or WITH, got {first or 'nothing'!r}")
    return out


def _matches(use: TableUse, banned: ObjectRef) -> bool:
    want = [p for p in (banned.catalog, banned.schema, banned.table) if p is not None]
    have = list(use.parts)
    if len(have) > len(want):
        return False
    # a shorter reference may omit leading parts: match from the right (conservative)
    for (text, quoted), target in zip(reversed(have), reversed(want), strict=False):
        if (text != target) if quoted else (text.casefold() != target.casefold()):
            return False
    return True


def check_sql(
    sql: str, dialect: str | None = None, *, never_query: Collection[ObjectRef] = ()
) -> GuardVerdict:
    """Verdict on one statement of agent SQL. ``dialect`` is accepted for forward compatibility."""
    if not isinstance(sql, str) or not sql.strip():
        return GuardVerdict(False, ("SG801: empty SQL",))
    if len(sql) > MAX_SQL_CHARS:
        return GuardVerdict(False, (f"SG801: longer than {MAX_SQL_CHARS} characters",))
    reasons: list[str] = []
    for backslash in (False, True):
        for r in _not_read_only(sql, backslash):
            if r not in reasons:
                reasons.append(r)
    if never_query:
        banned = [b.table_ref() for b in never_query]
        for backslash in (False, True):
            for use in _uses(sql, backslash):
                for b in banned:
                    if _matches(use, b):
                        r = f"SG802: reads never-query table {b.canonical()}"
                        if r not in reasons:
                            reasons.append(r)
    return GuardVerdict(not reasons, tuple(reasons))


def _uses(sql: str, backslash: bool) -> list[TableUse]:
    return table_uses(sql, backslash=backslash)
