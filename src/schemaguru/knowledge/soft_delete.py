"""A declared soft-delete filter checked against the catalog and measured values (spec 4.3
``SOFT_DELETE``, 15: expectations are checked, never silently applied).

A HUMAN or DECLARED live-rows predicate is an expectation like a declared enum. Without spending
anything, it is compared with what is already known:

* every column it names must exist: an explicit marker column, every quoted identifier, and at
  least one bare name (matched case-insensitively, as an unquoted name folds);
* when it names exactly ONE column whose complete value list was measured exactly, at least one
  value the predicate compares with must occur in that column (or the filter matches nothing it
  was written for), and ``TRUE``/``FALSE`` need a boolean column. NULLs are always allowed: an
  all-NULL marker means nothing was deleted yet.

Anything that cannot be judged (table not crawled, values unknown or sampled, several columns)
is not a mismatch. A mismatch is reported (the view warns, the SQL snippet drops the filter,
load_overlays emits ``SG210``); it never removes or edits the human's text.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone

from schemaguru.model.facts import Fact, Provenance, Status
from schemaguru.model.refs import ObjectRef
from schemaguru.model.structure import Column
from schemaguru.pii import mask_value
from schemaguru.store.protocol import Store

_STRING = re.compile(r"'((?:[^']|'')*)'")
_QUOTED_IDENT = re.compile(r'"((?:[^"]|"")*)"|`([^`]*)`')
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_NUMBER = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?(?![\w.])")
_BOOLEAN = re.compile(r"\b(?:TRUE|FALSE)\b", re.IGNORECASE)
_NEVER = datetime.min.replace(tzinfo=timezone.utc)
# words of a filter expression that are never column names (upper-cased)
_KEYWORDS = frozenset(
    {
        *("AND", "OR", "NOT", "IN", "IS", "NULL", "BETWEEN", "TRUE", "FALSE", "LIKE", "ILIKE"),
        *("DATE", "TIMESTAMP", "INTERVAL", "AS", "CAST", "CASE", "WHEN", "THEN", "ELSE", "END"),
        *("CURRENT_DATE", "CURRENT_TIMESTAMP", "CURRENT_TIME", "ESCAPE", "DISTINCT", "FROM"),
    }
)


def _literals(predicate: str) -> tuple[list[str], str]:
    """The string literals of ``predicate`` (unescaped) and the text with them blanked."""
    found = [m.group(1).replace("''", "'") for m in _STRING.finditer(predicate)]
    return found, _STRING.sub("''", predicate)


def mismatch(
    predicate: str,
    markers: Sequence[str] | None,
    columns: Mapping[str, str | None],
    observed: Mapping[str, Sequence[object]],
) -> str | None:
    """Why ``predicate`` does not fit the table, or ``None`` when it fits or cannot be judged.

    ``columns`` maps each catalog column name to its type family (empty: table not crawled);
    ``observed`` maps a column to its COMPLETE exactly measured values (see
    :func:`observed_values`). The reason names columns, never data values.
    """
    if not columns:
        return None
    for m in markers or ():
        if m not in columns:
            return f"its marker column {m!r} does not exist"
    by_fold: dict[str, list[str]] = {}
    for name in columns:
        by_fold.setdefault(name.casefold(), []).append(name)
    literals, code = _literals(predicate)
    named: set[str] = set()
    for q in _QUOTED_IDENT.finditer(code):
        ident = q.group(1).replace('""', '"') if q.group(1) is not None else q.group(2)
        if ident not in columns:
            return f'column "{ident}" does not exist'
        named.add(ident)
    bare = _QUOTED_IDENT.sub(" ", code)
    for w in _WORD.finditer(bare):
        word, rest = w.group(0), bare[w.end() :].lstrip()
        # a keyword, a function name, or the qualifier of ``t.col`` (the column part is kept)
        if word.upper() in _KEYWORDS or rest.startswith(("(", ".")):
            continue
        hits = by_fold.get(word.casefold(), [])
        exact = [h for h in hits if h == word]
        if exact or len(hits) == 1:
            named.add((exact or hits)[0])
    if not named:
        return "the filter names no column of the table"
    if len(named) != 1:
        return None  # a literal could belong to any of them
    (col,) = named
    family = columns.get(col)
    if _BOOLEAN.search(code) and family is not None and family != "boolean":
        return f"it compares {col} ({family}) with TRUE/FALSE"
    values = observed.get(col)
    compared = literals + _NUMBER.findall(code)
    if values is None or not compared or not values:
        return None
    known = {str(v) for v in values}
    known |= {mask_value(str(v))[0] for v in values}
    if not any(c in known or mask_value(c)[0] in known for c in compared):
        return f"the measured values of {col} include none it compares with"
    return None


def observed_values(facts: Iterable[Fact]) -> dict[str, list[object]]:
    """Column name -> the complete value list of its newest exact ``VALUES`` measurement.

    Only an exact, OBSERVED measurement that saw every distinct value counts; a top-k cut, a
    sample or a ``none``-policy fact (no values stored) leaves the column out.
    """
    newest: dict[str, Fact] = {}
    for f in facts:
        s = f.subject
        if (
            f.kind.name != "VALUES"
            or f.provenance is not Provenance.MEASURED
            or f.status is not Status.OBSERVED
            or f.method != "exact"
            or not isinstance(s, ObjectRef)
            or s.column is None
        ):
            continue
        old = newest.get(s.column)
        if old is None or (f.observed_at or _NEVER) > (old.observed_at or _NEVER):
            newest[s.column] = f
    out: dict[str, list[object]] = {}
    for name, f in sorted(newest.items()):
        obs = f.value.get("observed")
        distinct = f.value.get("distinct")
        if not isinstance(obs, list) or not isinstance(distinct, int):
            continue
        vals = [i.get("value") for i in obs if isinstance(i, dict)]
        if len(vals) == distinct:
            out[name] = vals
    return out


# the contradiction message a mismatch adds to the table's SOFT_DELETE knowledge (the renderer
# shows the reason after it)
MISMATCH = "soft-delete filter does not match the data: "


def fact_mismatch(f: Fact, columns: Iterable[Column], facts: Iterable[Fact]) -> str | None:
    """:func:`mismatch` for a HUMAN or DECLARED ``SOFT_DELETE`` fact of a table whose catalog
    ``columns`` and stored ``facts`` (its columns' ``VALUES`` among them) are given."""
    if f.kind.name != "SOFT_DELETE" or f.provenance not in (Provenance.HUMAN, Provenance.DECLARED):
        return None
    pred = f.value.get("predicate")
    if not isinstance(pred, str) or not pred.strip():
        return None
    markers = f.value.get("marker_columns")
    names = [str(m) for m in markers] if isinstance(markers, list) else None
    families = {c.ref.column: c.family for c in columns if c.ref.column is not None}
    return mismatch(pred, names, families, observed_values(facts))


def store_mismatch(store: Store, f: Fact) -> str | None:
    """:func:`fact_mismatch` reading the table's columns and values from ``store``."""
    if (
        f.kind.name != "SOFT_DELETE"
        or f.provenance not in (Provenance.HUMAN, Provenance.DECLARED)
        or not isinstance(f.subject, ObjectRef)
    ):
        return None  # nothing to check: no store reads
    cols = store.columns(f.subject.table_ref())
    return fact_mismatch(f, cols, store.facts_for([c.ref for c in cols], ["VALUES"]))
