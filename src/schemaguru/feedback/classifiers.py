"""Failure classifiers (spec 16.2): turn a failed query + error text into identifier-only records.

Each classifier is a plug-in :class:`FailureClassifier`. They never see a store: a record's
table is the reference as the SQL wrote it, and intake resolves it. Only identifiers are
produced; literals are blanked before any token is read.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from schemaguru._util.sqltext import NamePart, TableUse, Tok, blank, table_uses, tokenize
from schemaguru.model.refs import ObjectRef

FailureClass = Literal["type_mismatch", "unknown_column", "unknown_table", "ambiguous_column"]

_MAX_SQL = 200_000
_MAX_ERROR = 20_000
_NUMERIC = frozenset(
    [
        "int",
        "int2",
        "int4",
        "int8",
        "int64",
        "integer",
        "smallint",
        "bigint",
        "tinyint",
        "numeric",
        "decimal",
        "bignumeric",
        "float",
        "float4",
        "float8",
        "float32",
        "float64",
        "double",
        "real",
        "number",
        "hugeint",
    ]
)
_KEYWORDS = frozenset(
    [
        "NULL",
        "TRUE",
        "FALSE",
        "SELECT",
        "FROM",
        "WHERE",
        "AND",
        "OR",
        "NOT",
        "IN",
        "IS",
        "LIKE",
        "BETWEEN",
        "CASE",
        "WHEN",
        "THEN",
        "ELSE",
        "END",
        "AS",
        "DATE",
        "TIMESTAMP",
        "INTERVAL",
        "CAST",
        "ON",
        "JOIN",
    ]
)


@dataclass(frozen=True)
class FeedbackRecord:
    table: ObjectRef | None
    column: str | None
    failure_class: FailureClass


class FailureClassifier(Protocol):
    def classify(self, sql: str, error: str, dialect: str | None) -> list[FeedbackRecord]: ...


# ----- shared helpers ------------------------------------------------------------------------


def _split_name(text: str) -> list[str]:
    """Split ``a.b."c.d"`` on dots outside quotes and strip quotes (``:`` counts as a dot)."""
    parts: list[str] = []
    buf: list[str] = []
    quote = ""
    text = text.strip()
    if len(text) > 2 and text[0] in "\"'`" and text.count(text[0]) == 2 and text[-1] == text[0]:
        text = text[1:-1]  # one quoted token holding a dotted name, as Postgres prints it
    for ch in text:
        if quote:
            if ch == quote:
                quote = ""
            else:
                buf.append(ch)
        elif ch in "\"'`":
            quote = ch
        elif ch in ".:":
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return [p for p in parts if p]


def _ref(parts: Sequence[str], column: str | None = None) -> ObjectRef | None:
    try:
        if len(parts) == 1:
            return ObjectRef(None, None, parts[0], column)
        if len(parts) == 2:
            return ObjectRef(None, parts[0], parts[1], column)
        if len(parts) == 3:
            return ObjectRef(parts[0], parts[1], parts[2], column)
    except ValueError:
        pass
    return None


def _ref_of_use(use: TableUse) -> ObjectRef | None:
    return _ref([p for p, _ in use.parts])


def _uses(sql: str) -> list[TableUse]:
    return table_uses(sql)


def _attribute(sql: str, name: Sequence[str]) -> tuple[ObjectRef | None, str] | None:
    """Table + column for a possibly qualified column name, per the attribution rules."""
    if not name:
        return None
    column = name[-1]
    qual = [q.casefold() for q in name[:-1]]
    uses = _uses(sql)
    if not qual:
        distinct = {tuple(p.casefold() for p, _ in u.parts) for u in uses}
        if len(distinct) != 1:
            return None  # a bare column is attributed only when exactly one table is read
        return _ref_of_use(uses[0]), column
    matches = [
        u
        for u in uses
        if (u.alias is not None and len(qual) == 1 and u.alias.casefold() == qual[0])
        or [p.casefold() for p, _ in u.parts][-len(qual) :] == qual
    ]
    if len({tuple(p.casefold() for p, _ in u.parts) for u in matches}) != 1:
        return None
    return _ref_of_use(matches[0]), column


def _view(sql: str) -> str:
    return blank(sql, blank_quotes="'")[0]


def _offset(sql: str, line: int, col: int) -> int | None:
    lines = sql.split("\n")
    if not 1 <= line <= len(lines) or col < 1:
        return None
    return sum(len(x) + 1 for x in lines[: line - 1]) + col - 1


_BQ_POS = re.compile(r"\[(\d+):(\d+)\]")
_PG_LINE = re.compile(r"LINE (\d+): ([^\n]*)\n( *)\^")
_CHAR_POS = re.compile(r"(?:at (?:position|character) |Position: )(\d+)", re.IGNORECASE)


def _error_offset(sql: str, error: str) -> int | None:
    m = _BQ_POS.search(error)
    if m:
        return _offset(sql, int(m.group(1)), int(m.group(2)))
    m = _PG_LINE.search(error)
    if m:
        line_no = int(m.group(1))
        shown = m.group(2)
        caret = len(m.group(3)) - len(f"LINE {line_no}: ")
        lines = sql.split("\n")
        if not 1 <= line_no <= len(lines) or caret < 0:
            return None
        core = shown.strip(".")
        shift = lines[line_no - 1].find(core) - (len(shown) - len(shown.lstrip(".")))
        if lines[line_no - 1].find(core) < 0:
            return None
        return _offset(
            sql, line_no, 1 + caret + max(shift, 0) if shown.startswith("...") else 1 + caret
        )
    m = _CHAR_POS.search(error)
    if m and int(m.group(1)) >= 1:
        return int(m.group(1)) - 1
    return None


def _is_name_tok(t: Tok) -> bool:
    return t.kind == "qident" or (t.kind == "ident" and t.text.upper() not in _KEYWORDS)


def _name_from(toks: list[Tok], i: int) -> list[str] | None:
    """Dotted column name starting at token ``i`` (not a function call)."""
    parts: list[str] = []
    while i < len(toks) and _is_name_tok(toks[i]):
        parts.append(toks[i].text.strip('"`'))
        if i + 2 < len(toks) and toks[i + 1].text == "." and _is_name_tok(toks[i + 2]):
            i += 2
            continue
        if i + 1 < len(toks) and toks[i + 1].text == "(":
            return None
        return parts
    return None


def _name_ending(toks: list[Tok], j: int) -> list[str] | None:
    while j >= 2 and toks[j - 1].text == "." and _is_name_tok(toks[j - 2]):
        j -= 2
    return _name_from(toks, j) if j >= 0 and _is_name_tok(toks[j]) else None


# ----- type_mismatch ---------------------------------------------------------------------------

_TYPE_TRIGGER = re.compile(
    r"No matching signature|operator does not exist|Cannot compare values of type"
    r"|Could not choose a best candidate operator",
    re.IGNORECASE,
)
_BQ_TYPES = re.compile(r"argument types: ([^\n]*?)(?: at \[|\.?$|\n)", re.IGNORECASE)
_PG_TYPES = re.compile(r"operator does not exist: ([A-Za-z0-9_ \[\]]+?) \S+ ([A-Za-z0-9_ \[\]]+)")
_DUCK_TYPES = re.compile(r"type (\w+) and type (\w+)", re.IGNORECASE)


def _types(error: str) -> list[str]:
    for rx in (_BQ_TYPES, _PG_TYPES, _DUCK_TYPES):
        m = rx.search(error)
        if m:
            raw = m.group(1).split(",") if rx is _BQ_TYPES else list(m.groups())
            return [t.strip().casefold().split("(")[0].strip() for t in raw if t.strip()]
    return []


class TypeMismatchClassifier:
    def classify(self, sql: str, error: str, dialect: str | None) -> list[FeedbackRecord]:
        if not _TYPE_TRIGGER.search(error):
            return []
        types = _types(error)
        if types and all(t in _NUMERIC for t in types):
            return []  # a numeric pair is not a schema-knowledge problem
        off = _error_offset(sql, error)
        if off is None:
            return []
        toks = tokenize(_view(sql))
        idx = next((i for i, t in enumerate(toks) if t.start <= off < t.end), None)
        if idx is None:
            idx = next((i for i, t in enumerate(toks) if t.start >= off), None)
        if idx is None:
            return []
        cands: list[list[str] | None] = []
        t = toks[idx]
        if t.kind == "punct" and idx > 0:  # position of the operator (Postgres)
            cands.append(_name_ending(toks, idx - 1))
            cands.append(_name_from(toks, idx + 1) if idx + 1 < len(toks) else None)
        else:  # start of the left operand (BigQuery)
            cands.append(_name_from(toks, idx))
            j = idx
            while j < len(toks) and not (toks[j].kind == "punct" and toks[j].text not in ".(),"):
                j += 1
            cands.append(_name_from(toks, j + 1) if j + 1 < len(toks) else None)
        for name in cands:
            if name:
                att = _attribute(sql, name)
                if att is not None:
                    return [FeedbackRecord(att[0], att[1], "type_mismatch")]
                return []
        return []


# ----- unknown_column / ambiguous_column / unknown_table ----------------------------------------

_Q = r"""("[^"]+"|'[^']+'|`[^`]+`|[\w$]+(?:\.[\w$]+)*)"""
_COL_OF_REL = re.compile(rf"column {_Q} of relation {_Q} does not exist", re.IGNORECASE)
_UNKNOWN_COL = [
    re.compile(rf"column {_Q} does not exist", re.IGNORECASE),
    re.compile(r"Unrecognized name: ([\w$.]+)", re.IGNORECASE),
    re.compile(rf"Unknown column {_Q} in", re.IGNORECASE),
    re.compile(r"no such column: ([\w$.]+)", re.IGNORECASE),
    re.compile(rf"Referenced column {_Q} not found", re.IGNORECASE),
    re.compile(rf'Table "[^"]+" does not have a column named {_Q}', re.IGNORECASE),
    re.compile(rf"invalid identifier {_Q}", re.IGNORECASE),
]
_AMBIGUOUS = [
    re.compile(rf"column reference {_Q} is ambiguous", re.IGNORECASE),
    re.compile(r"Column name ([\w$.]+) is ambiguous", re.IGNORECASE),
    re.compile(rf"Ambiguous reference to column name {_Q}", re.IGNORECASE),
    re.compile(rf"Column {_Q} in \w[\w ]* is ambiguous", re.IGNORECASE),
]
_UNKNOWN_TABLE = [
    re.compile(rf"(?<!of )relation {_Q} does not exist", re.IGNORECASE),
    re.compile(r"Not found: Table ([^\s]+) was not found", re.IGNORECASE),
    re.compile(rf"Table {_Q} doesn't exist", re.IGNORECASE),
    re.compile(r"no such table: ([\w$.]+)", re.IGNORECASE),
    re.compile(r"Table with name ([\w$.]+) does not exist", re.IGNORECASE),
    re.compile(rf"Object {_Q} does not exist or not authorized", re.IGNORECASE),
]


def _first(rxs: Sequence[re.Pattern[str]], error: str) -> str | None:
    for rx in rxs:
        m = rx.search(error)
        if m:
            return m.group(1)
    return None


class UnknownColumnClassifier:
    def classify(self, sql: str, error: str, dialect: str | None) -> list[FeedbackRecord]:
        m = _COL_OF_REL.search(error)
        if m:
            ref = _ref(_split_name(m.group(2)), None)
            col = _split_name(m.group(1))
            if ref is not None and col:
                return [FeedbackRecord(ref, col[-1], "unknown_column")]
            return []
        text = _first(_UNKNOWN_COL, error)
        att = _attribute(sql, _split_name(text)) if text else None
        return [FeedbackRecord(att[0], att[1], "unknown_column")] if att else []


class AmbiguousColumnClassifier:
    """Every table the query reads is a candidate; intake keeps those that hold the column."""

    def classify(self, sql: str, error: str, dialect: str | None) -> list[FeedbackRecord]:
        text = _first(_AMBIGUOUS, error)
        name = _split_name(text) if text else []
        if not name:
            return []
        if len(name) > 1:
            att = _attribute(sql, name)
            return [FeedbackRecord(att[0], att[1], "ambiguous_column")] if att else []
        out: list[FeedbackRecord] = []
        seen: set[ObjectRef] = set()
        for u in _uses(sql):
            ref = _ref_of_use(u)
            if ref is not None and ref not in seen:
                seen.add(ref)
                out.append(FeedbackRecord(ref, name[0], "ambiguous_column"))
        return out


class UnknownTableClassifier:
    def classify(self, sql: str, error: str, dialect: str | None) -> list[FeedbackRecord]:
        text = _first(_UNKNOWN_TABLE, error)
        ref = _ref(_split_name(text)) if text else None
        return [FeedbackRecord(ref, None, "unknown_table")] if ref else []


BUILTIN_CLASSIFIERS: tuple[FailureClassifier, ...] = (
    TypeMismatchClassifier(),
    UnknownColumnClassifier(),
    UnknownTableClassifier(),
    AmbiguousColumnClassifier(),
)


def classify_all(
    sql: str,
    error: str,
    dialect: str | None = None,
    classifiers: Sequence[FailureClassifier] | None = None,
) -> list[FeedbackRecord]:
    """Run every classifier; one that raises contributes nothing. Never raises."""
    out: list[FeedbackRecord] = []
    try:
        if not isinstance(sql, str) or not isinstance(error, str):
            return out
        sql, error = sql[:_MAX_SQL], error[:_MAX_ERROR]
    except Exception:
        return out
    for c in BUILTIN_CLASSIFIERS if classifiers is None else classifiers:
        try:
            for rec in c.classify(sql, error, dialect):
                if rec not in out:
                    out.append(rec)
        except Exception:
            continue
    return out


__all__ = [
    "BUILTIN_CLASSIFIERS",
    "FailureClass",
    "FailureClassifier",
    "FeedbackRecord",
    "NamePart",
    "classify_all",
]
