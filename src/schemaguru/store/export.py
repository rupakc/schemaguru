"""Sorted, byte-deterministic JSON export / import of a store's knowledge (spec 7.3).

Exported: schemas, tables (with their columns and constraints, withdrawn ones included) and
facts. Operational state (snapshots, crawl state, ledger, queue, feedback) is not exported.
Streams table by table, so memory is bounded by one table.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import TextIO

from schemaguru._util.hashing import canonical_json
from schemaguru.model.facts import Fact
from schemaguru.model.structure import Column, Constraint, Schema, Table
from schemaguru.store.protocol import Store

FORMAT = "schemaguru-export"
VERSION = 1

# the one encoder every store writes with: an exported fact is its stored text, byte for byte
_dumps = canonical_json


def export_json(store: Store, path: str | os.PathLike[str]) -> None:
    """Write via a temp file and rename, so a failed export never leaves a partial file.

    The temp file is unique (in the target's directory, so the rename is atomic), so two
    exports to one path never write into each other's file; the last rename wins with a whole
    export. It is created with the process umask, like any file the caller writes, and
    removed if the export fails.
    """
    target = Path(path)
    while True:
        tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            out = tmp.open("x", encoding="utf-8", newline="\n")
        except FileExistsError:  # pragma: no cover - a 128-bit name collision
            continue
        break
    try:
        with out:
            _write(store, out)
        _replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


_REPLACE_ATTEMPTS = 10


def _replace(src: Path, dst: Path) -> None:
    """``os.replace``, retried on ``PermissionError``: on Windows a rename onto a file that
    another export is replacing or a reader has open fails transiently. Bounded (10 attempts,
    5 ms doubling to 100 ms), then the error propagates."""
    delay = 0.005
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.1)


def _write(store: Store, out: TextIO) -> None:
    out.write('{"facts":[')
    for i, f in enumerate(store.iter_facts()):
        out.write(("," if i else "") + _dumps(f.to_dict()))
    out.write(f'],"format":"{FORMAT}","schemas":[')
    for i, s in enumerate(store.iter_schemas()):
        out.write(("," if i else "") + _dumps(s.to_dict()))
    out.write('],"tables":[')
    for i, t in enumerate(store.iter_tables(include_withdrawn=True)):
        entry = t.to_dict()
        entry["columns"] = [c.to_dict() for c in store.columns(t.ref)]
        entry["constraints"] = sorted((c.to_dict() for c in store.constraints(t.ref)), key=_dumps)
        out.write(("," if i else "") + _dumps(entry))
    out.write(f'],"version":{VERSION}}}\n')


def import_json(store: Store, path: str | os.PathLike[str]) -> None:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if doc.get("format") != FORMAT or doc.get("version") != VERSION:
        raise ValueError(f"not a {FORMAT} v{VERSION} file: {path}")
    with store.transaction():
        store.upsert_schemas(Schema.from_dict(s) for s in doc["schemas"])
        for entry in doc["tables"]:
            columns = [Column.from_dict(c) for c in entry.pop("columns")]
            constraints = [Constraint.from_dict(c) for c in entry.pop("constraints")]
            table = Table.from_dict(entry)
            store.upsert_tables([table])
            store.upsert_columns(table.ref, columns)
            store.upsert_constraints(table.ref, constraints)
        store.put_facts(Fact.from_dict(f) for f in doc["facts"])
