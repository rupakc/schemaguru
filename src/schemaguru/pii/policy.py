"""Which data values may reach an LLM (spec 11): per-table mode from config globs."""

from __future__ import annotations

from collections.abc import Iterable
from fnmatch import fnmatchcase
from functools import lru_cache
from typing import Literal, cast

from schemaguru.config import SamplingConfig
from schemaguru.diagnostics import Diagnostic, Severity
from schemaguru.errors import ConfigError
from schemaguru.model.refs import ObjectRef

Mode = Literal["masked", "raw", "none"]
_MODES = ("masked", "raw", "none")


# ---- glob containment (real, not textual) -------------------------------------------------
# A glob is compiled to a small NFA over tokens; ``_contains(a, b)`` decides language(a) <=
# language(b) by exploring pairs of NFA state sets over one representative character of every
# class of characters the two globs cannot tell apart (see ``_alphabet``).

_Tok = tuple[str, object]  # ("lit", c) | ("any", None) | ("star", None) | ("cls", (neg, items))


def _parse(glob: str) -> tuple[_Tok, ...]:
    toks: list[_Tok] = []
    i, n = 0, len(glob)
    while i < n:
        c = glob[i]
        if c == "*":
            if not toks or toks[-1][0] != "star":
                toks.append(("star", None))
        elif c == "?":
            toks.append(("any", None))
        elif c == "[":
            j = i + 1
            neg = j < n and glob[j] == "!"
            if neg:
                j += 1
            k = j + 1 if j < n and glob[j] == "]" else j
            while k < n and glob[k] != "]":
                k += 1
            if k >= n:
                toks.append(("lit", "["))
            else:
                toks.append(("cls", (neg, glob[j:k])))
                i = k
        else:
            toks.append(("lit", c))
        i += 1
    return tuple(toks)


def _cls_ranges(body: str) -> list[tuple[str, str]]:
    """A class body (``a-fx``) as inclusive ranges (``[("a", "f"), ("x", "x")]``)."""
    out: list[tuple[str, str]] = []
    i = 0
    while i < len(body):
        if i + 2 < len(body) and body[i + 1] == "-":
            out.append((body[i], body[i + 2]))
            i += 3
        else:
            out.append((body[i], body[i]))
            i += 1
    return out


def _cls_has(spec: tuple[bool, str], ch: str) -> bool:
    neg, body = spec
    return any(lo <= ch <= hi for lo, hi in _cls_ranges(body)) != neg


def _alphabet(*globs: tuple[_Tok, ...]) -> list[str]:
    """One character per interval of code points on which every token of ``globs`` agrees.

    Each literal ``c`` and each class range ``lo-hi`` only changes its answer at ``c`` / ``c+1``
    and ``lo`` / ``hi+1``; between two such break points every character behaves alike, so the
    break points (and ``\\x00`` for everything below them) are a complete alphabet. Listing only
    the characters written in the globs would miss a range's interior (``c`` in ``[a-f]``).
    """
    points = {0}
    for toks in globs:
        for kind, arg in toks:
            if kind == "lit":
                points |= {ord(cast(str, arg)), ord(cast(str, arg)) + 1}
            elif kind == "cls":
                for lo, hi in _cls_ranges(cast(tuple[bool, str], arg)[1]):
                    points |= {ord(lo), ord(hi) + 1}
    return [chr(p) for p in sorted(points) if p <= 0x10FFFF]


def _closure(toks: tuple[_Tok, ...], states: frozenset[int]) -> frozenset[int]:
    out = set(states)
    for s in states:
        j = s
        while j < len(toks) and toks[j][0] == "star":
            j += 1
            out.add(j)
    return frozenset(out)


def _step(toks: tuple[_Tok, ...], states: frozenset[int], ch: str) -> frozenset[int]:
    nxt: set[int] = set()
    for s in states:
        if s >= len(toks):
            continue
        kind, arg = toks[s]
        if kind == "star":
            nxt.add(s)
        elif (
            kind == "any"
            or (kind == "lit" and arg == ch)
            or (kind == "cls" and _cls_has(cast(tuple[bool, str], arg), ch))
        ):
            nxt.add(s + 1)
    return _closure(toks, frozenset(nxt))


@lru_cache(maxsize=4096)
def _contains(a: str, b: str) -> bool:
    """True when every string matched by glob ``a`` is also matched by glob ``b``."""
    ta, tb = _parse(a), _parse(b)
    alphabet = _alphabet(ta, tb)
    start = (_closure(ta, frozenset({0})), _closure(tb, frozenset({0})))
    seen = {start}
    todo = [start]
    while todo:
        sa, sb = todo.pop()
        if len(ta) in sa and len(tb) not in sb:
            return False
        for ch in alphabet:
            na = _step(ta, sa, ch)
            if not na:
                continue
            pair = (na, _step(tb, sb, ch))
            if pair not in seen:
                seen.add(pair)
                todo.append(pair)
    return True


_RESTRICTION = {"none": 0, "masked": 1, "raw": 2}  # lower is more restrictive


def glob_matches(glob: str, ref: ObjectRef) -> bool:
    """Whether a ``per_glob`` key covers ``ref``'s table (case-insensitive; see ValuePolicy)."""
    table = ref.table_ref()
    names = [table.canonical()]
    if table.catalog is not None:
        names.append(ObjectRef(None, table.schema, table.table).canonical())
    if "." not in glob:
        names.append(table.table)
    folded = glob.casefold()
    return any(fnmatchcase(n.casefold(), folded) for n in names)


def lint_per_glob(cfg: SamplingConfig, tables: Iterable[ObjectRef]) -> list[Diagnostic]:
    """``SG507`` (warning) for every ``per_glob`` key that matches none of ``tables``: a typo
    there silently leaves the tables it meant to restrict on the default mode."""
    pending = dict.fromkeys(cfg.per_glob)
    for ref in tables:
        if not pending:
            break
        for glob in [g for g in pending if glob_matches(g, ref)]:
            del pending[glob]
    return [
        Diagnostic(
            "SG507",
            Severity.WARNING,
            f"sampling.per_glob[{glob!r}] matches no stored table; its mode "
            f"{cfg.per_glob[glob]!r} applies to nothing (globs are case-insensitive)",
            location=f"sampling.per_glob[{glob!r}]",
            hint="check the glob against `schemaguru search` or the crawled names",
        )
        for glob in pending
    ]


class ValuePolicy:
    """Resolve the value mode for a table.

    ``cfg.per_glob`` maps a glob to a mode. A glob is tested (``fnmatch``, ``*`` crossing dots,
    **case-insensitive**: both sides are case-folded, so ``hr.*`` covers Snowflake's
    ``DB.HR.EMPLOYEES``; a case-sensitive match would fail open) against the table's canonical
    name and, when a catalog is present, against ``schema.table``; the bare table name is
    tested only by globs that contain no dot.

    Among matching globs, A is *narrower* than B when A's language is strictly contained in
    B's (``shop.cust*`` is narrower than ``shop.*``; decided by a real containment check, not
    by matching one glob's text against the other). The narrowest globs win; if several remain
    (equal or incomparable, e.g. ``shop.*`` vs ``*.customers``) the **most restrictive mode
    wins** (none > masked > raw), so an ambiguous config can never widen access. No match gives
    ``cfg.llm_values``.
    """

    @staticmethod
    def for_table(ref: ObjectRef, cfg: SamplingConfig) -> Mode:
        matched: list[str] = []
        for glob, mode in cfg.per_glob.items():
            if mode not in _MODES:
                raise ConfigError(
                    f"sampling.per_glob[{glob!r}]: {mode!r} is not one of {list(_MODES)}"
                )
            if glob_matches(glob, ref):
                matched.append(glob)
        if not matched:
            return cfg.llm_values

        def narrower(a: str, b: str) -> bool:
            fa, fb = a.casefold(), b.casefold()
            return fa != fb and _contains(fa, fb) and not _contains(fb, fa)

        top = [g for g in matched if not any(narrower(o, g) for o in matched)]
        return cast(Mode, min((cfg.per_glob[g] for g in top), key=_RESTRICTION.__getitem__))
