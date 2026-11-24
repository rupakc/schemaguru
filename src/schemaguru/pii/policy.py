"""Which data values may reach an LLM (spec 11): per-table mode from config globs."""

from __future__ import annotations

from fnmatch import fnmatchcase
from functools import lru_cache
from typing import Literal, cast

from schemaguru.config import SamplingConfig
from schemaguru.errors import ConfigError
from schemaguru.model.refs import ObjectRef

Mode = Literal["masked", "raw", "none"]
_MODES = ("masked", "raw", "none")


# ---- glob containment (real, not textual) -------------------------------------------------
# A glob is compiled to a small NFA over tokens; ``_contains(a, b)`` decides language(a) <=
# language(b) by exploring pairs of NFA state sets over the alphabet of both globs' characters
# plus one fresh character standing for "any other".

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


def _cls_has(spec: tuple[bool, str], ch: str) -> bool:
    neg, body = spec
    hit = False
    i = 0
    while i < len(body):
        if i + 2 < len(body) and body[i + 1] == "-":
            hit = hit or body[i] <= ch <= body[i + 2]
            i += 3
        else:
            hit = hit or body[i] == ch
            i += 1
    return hit != neg


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
            kind == "any" or (kind == "lit" and arg == ch) or (kind == "cls" and _cls_has(arg, ch))  # type: ignore[arg-type]
        ):
            nxt.add(s + 1)
    return _closure(toks, frozenset(nxt))


@lru_cache(maxsize=4096)
def _contains(a: str, b: str) -> bool:
    """True when every string matched by glob ``a`` is also matched by glob ``b``."""
    ta, tb = _parse(a), _parse(b)
    alphabet = [*sorted({*a, *b}), "\x00"]
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


class ValuePolicy:
    """Resolve the value mode for a table.

    ``cfg.per_glob`` maps a glob to a mode. A glob is tested (case-sensitive ``fnmatch``,
    ``*`` crossing dots) against the table's canonical name and, when a catalog is present,
    against ``schema.table``; the bare table name is tested only by globs that contain no dot.

    Among matching globs, A is *narrower* than B when A's language is strictly contained in
    B's (``shop.cust*`` is narrower than ``shop.*``; decided by a real containment check, not
    by matching one glob's text against the other). The narrowest globs win; if several remain
    (equal or incomparable, e.g. ``shop.*`` vs ``*.customers``) the **most restrictive mode
    wins** (none > masked > raw), so an ambiguous config can never widen access. No match gives
    ``cfg.llm_values``.
    """

    @staticmethod
    def for_table(ref: ObjectRef, cfg: SamplingConfig) -> Mode:
        table = ref.table_ref()
        qualified = [table.canonical()]
        if table.catalog is not None:
            qualified.append(ObjectRef(None, table.schema, table.table).canonical())
        matched: list[str] = []
        for glob, mode in cfg.per_glob.items():
            if mode not in _MODES:
                raise ConfigError(
                    f"sampling.per_glob[{glob!r}]: {mode!r} is not one of {list(_MODES)}"
                )
            names = qualified if "." in glob else [*qualified, table.table]
            if any(fnmatchcase(n, glob) for n in names):
                matched.append(glob)
        if not matched:
            return cfg.llm_values

        def narrower(a: str, b: str) -> bool:
            return a != b and _contains(a, b) and not _contains(b, a)

        top = [g for g in matched if not any(narrower(o, g) for o in matched)]
        return cast(Mode, min((cfg.per_glob[g] for g in top), key=_RESTRICTION.__getitem__))
