"""Sorted, byte-deterministic JSON export / import of a store's knowledge (spec 7.3).

Exported: schemas, tables (with their columns and constraints, withdrawn ones included) and
facts. Operational state (snapshots, crawl state, ledger, queue, feedback) is not exported.
Streams table by table, so memory is bounded by one table.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from schemaguru._util.hashing import canonical_json
from schemaguru.model.facts import Fact
from schemaguru.model.structure import Column, Constraint, Schema, Table
from schemaguru.store.protocol import Store

FORMAT = "schemaguru-export"
VERSION = 1


_dumps = canonical_json


def export_json(store: Store, path: str | os.PathLike[str]) -> None:
    """Write via a temp file and rename, so a failed export never leaves a partial file."""
    target = Path(path)
    tmp = target.with_name(target.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as out:
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
            entry["constraints"] = sorted(
                (c.to_dict() for c in store.constraints(t.ref)), key=_dumps
            )
            out.write(("," if i else "") + _dumps(entry))
        out.write(f'],"version":{VERSION}}}\n')
    os.replace(tmp, target)


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
