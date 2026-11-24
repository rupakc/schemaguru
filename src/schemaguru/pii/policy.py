"""Which data values may reach an LLM (spec 11): per-table mode from config globs."""

from __future__ import annotations

import re
from fnmatch import fnmatchcase
from typing import Literal, cast

from schemaguru.config import SamplingConfig
from schemaguru.errors import ConfigError
from schemaguru.model.refs import ObjectRef

Mode = Literal["masked", "raw", "none"]
_MODES = ("masked", "raw", "none")


def _specificity(glob: str) -> tuple[int, int]:
    """Higher is more specific: longest literal prefix, then fewer wildcard characters."""
    m = re.search(r"[*?\[]", glob)
    prefix = len(glob) if m is None else m.start()
    wildcards = sum(glob.count(c) for c in "*?[")
    return prefix, -wildcards


class ValuePolicy:
    """Resolve the value mode for a table.

    ``cfg.per_glob`` maps a glob to a mode. A glob is tested (case-sensitive ``fnmatch``,
    ``*`` crossing dots) against the table's canonical name and, when a catalog is present,
    against ``schema.table`` and the bare table name too. The most specific matching glob wins:
    **longest literal (non-wildcard) prefix, ties broken by fewer wildcard characters, then
    lexicographic order** so the result is deterministic. No match gives ``cfg.llm_values``.
    """

    @staticmethod
    def for_table(ref: ObjectRef, cfg: SamplingConfig) -> Mode:
        table = ref.table_ref()
        names = [table.canonical()]
        if table.catalog is not None:
            names.append(ObjectRef(None, table.schema, table.table).canonical())
        names.append(table.table)
        best: tuple[tuple[int, int], str, str] | None = None
        for glob, mode in cfg.per_glob.items():
            if mode not in _MODES:
                raise ConfigError(
                    f"sampling.per_glob[{glob!r}]: {mode!r} is not one of {list(_MODES)}"
                )
            if not any(fnmatchcase(n, glob) for n in names):
                continue
            key = _specificity(glob)
            # sort key: higher specificity first, then glob text ascending
            if best is None or key > best[0] or (key == best[0] and glob < best[1]):
                best = (key, glob, mode)
        return cast(Mode, best[2] if best else cfg.llm_values)
