"""Which data values may reach an LLM (spec 11): per-table mode from config globs."""

from __future__ import annotations

from fnmatch import fnmatchcase
from typing import Literal, cast

from schemaguru.config import SamplingConfig
from schemaguru.errors import ConfigError
from schemaguru.model.refs import ObjectRef

Mode = Literal["masked", "raw", "none"]
_MODES = ("masked", "raw", "none")


_RESTRICTION = {"none": 0, "masked": 1, "raw": 2}  # lower is more restrictive


class ValuePolicy:
    """Resolve the value mode for a table.

    ``cfg.per_glob`` maps a glob to a mode. A glob is tested (case-sensitive ``fnmatch``,
    ``*`` crossing dots) against the table's canonical name and, when a catalog is present,
    against ``schema.table``; the bare table name is tested only by globs that contain no dot.

    Among matching globs, A is *narrower* than B when B matches A's own text (``shop.cust*`` is
    narrower than ``shop.*``) and not the reverse. The narrowest globs win; if several remain
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
            return a != b and fnmatchcase(a, b) and not fnmatchcase(b, a)

        top = [g for g in matched if not any(narrower(o, g) for o in matched)]
        return cast(Mode, min((cfg.per_glob[g] for g in top), key=_RESTRICTION.__getitem__))
