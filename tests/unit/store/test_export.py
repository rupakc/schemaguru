from __future__ import annotations

import random
from datetime import datetime, timezone
from pathlib import Path

from schemaguru.model.facts import Fact, Provenance, Status
from schemaguru.model.kinds import FactKind
from schemaguru.model.refs import ObjectRef
from schemaguru.model.structure import Column, Constraint, Schema, Table
from schemaguru.store.export import export_json, import_json
from schemaguru.store.sqlite import SQLiteStore

NOW = datetime(2026, 11, 24, tzinfo=timezone.utc)


def _fill(s: SQLiteStore, seed: int) -> None:
    rng = random.Random(seed)
    names = ["orders", "customers", "order_items", "Order Items", "events"]
    rng.shuffle(names)
    s.upsert_schemas([Schema(None, "shop", "the shop", 5)])
    for n in names:
        ref = ObjectRef(None, "shop", n)
        s.upsert_tables([Table(ref, "table", comment=f"{n} comment", row_count=3, created_at=NOW)])
        cols = [Column(ObjectRef(None, "shop", n, c), i, "STRING", "STRING", None, None, None)
                for i, c in enumerate(["id", "x"], 1)]  # fmt: skip
        s.upsert_columns(ref, cols)
        cons = [
            Constraint("unique", ref, ("x",)),
            Constraint("primary_key", ref, ("id",), enforced=True),
        ]
        rng.shuffle(cons)
        s.upsert_constraints(ref, cons)
        s.put_facts(
            [Fact.make(FactKind.ROW_COUNT, ref, {"rows": 3}, Provenance.CATALOG, Status.OBSERVED)]
        )
    s.mark_withdrawn([ObjectRef(None, "shop", "events")], NOW)


def test_export_byte_deterministic(tmp_path: Path) -> None:
    outs = []
    for seed in (1, 2):
        s = SQLiteStore(tmp_path / f"s{seed}.db")
        _fill(s, seed)
        a, b = tmp_path / f"a{seed}.json", tmp_path / f"b{seed}.json"
        export_json(s, a)
        export_json(s, b)
        assert a.read_bytes() == b.read_bytes()
        outs.append(a.read_bytes())
        s.close()
    assert outs[0] == outs[1]
    assert outs[0].endswith(b"\n")


def test_import_roundtrip(tmp_path: Path) -> None:
    s = SQLiteStore(tmp_path / "a.db")
    _fill(s, 3)
    p = tmp_path / "x.json"
    export_json(s, p)
    t = SQLiteStore(tmp_path / "b.db")
    import_json(t, p)
    p2 = tmp_path / "y.json"
    export_json(t, p2)
    assert p.read_bytes() == p2.read_bytes()
    assert t.table(ObjectRef(None, "shop", "events")).withdrawn_at == NOW  # type: ignore[union-attr]
    assert next(r.table for r, _ in t.search("customers", 3)) == "customers"
    s.close()
    t.close()
