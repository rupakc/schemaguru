"""Read-only SQL guard helper (layer 6, spec 16.4). It judges text; it never executes.

This is a conservative DENY-LIST over text, NOT a sandbox. It is dialect-agnostic: the SQL is
lexed under every plausible comment/quote/escape convention and refused if ANY reading shows a
violation (a second statement, a DML/DDL keyword, an unterminated quote or comment, a ``/*!``
conditional comment), so some valid SQL is refused on purpose. It does not stop side-effecting
or resource-heavy read-only constructs (``pg_sleep``, ``dblink``, ``pg_read_file``, expensive
scans): the host must still run queries under a read-only database role with limits.

``never_query`` is enforced by name: any identifier outside a string or comment whose text equals
the banned table's last name part (case-insensitively, quoted or not) refuses the SQL, wherever
it appears and whatever catalog/schema qualifies it. A string literal handed to ``IDENTIFIER(``
or ``TABLE(`` is refused as unresolvable. The ``dialect`` argument is reserved.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection
from dataclasses import dataclass

from schemaguru._util.sqltext import GUARD_VARIANTS, blank, tokenize
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


def check_sql(
    sql: str, dialect: str | None = None, *, never_query: Collection[ObjectRef] = ()
) -> GuardVerdict:
    """Verdict on one statement of agent SQL (see the module docstring for the limits)."""
    if not isinstance(sql, str) or not sql.strip():
        return GuardVerdict(False, ("SG801: empty SQL",))
    if len(sql) > MAX_SQL_CHARS:
        return GuardVerdict(False, (f"SG801: longer than {MAX_SQL_CHARS} characters",))
    banned = {b.table.casefold(): b.table_ref() for b in never_query}
    reasons: list[str] = []

    def add(r: str) -> None:
        if r not in reasons:
            reasons.append(r)

    for o in GUARD_VARIANTS:
        code = blank(sql, o, keep_idents=False)
        if code.bang:
            add("SG801: '/*!' conditional comments are not allowed")
        if not code.terminated:
            add("SG801: unterminated quote or comment (or an ambiguous escape)")
        text = code.view.strip()
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
        if banned:
            _check_banned(blank(sql, o).view, banned, add)
    return GuardVerdict(not reasons, tuple(reasons))


def _check_banned(view: str, banned: dict[str, ObjectRef], add: Callable[[str], None]) -> None:
    toks = tokenize(view)
    for i, t in enumerate(toks):
        if t.kind in ("ident", "qident"):
            text = t.text.strip('"`')
            names = {text.casefold()} | {p.casefold() for p in text.split(".")}
            for key, ref in banned.items():
                if key in names:
                    add(f"SG802: reads never-query table {ref.canonical()}")
            if (
                t.kind == "ident"
                and t.text.upper() in ("IDENTIFIER", "TABLE")
                and i + 2 < len(toks)
                and toks[i + 1].text == "("
                and toks[i + 2].kind == "str"
            ):
                add("SG802: a string given to IDENTIFIER( or TABLE( cannot be checked")
