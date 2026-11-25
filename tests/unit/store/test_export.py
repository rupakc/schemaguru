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


def test_concurrent_exports_do_not_clash(tmp_path: Path) -> None:
    import threading

    s = SQLiteStore(tmp_path / "c.db")
    _fill(s, 4)
    real = s.iter_facts
    both_open = threading.Barrier(2, timeout=10)

    def iter_facts():  # type: ignore[no-untyped-def]
        both_open.wait()  # both exports have opened their temp file before either writes
        return real()

    s.iter_facts = iter_facts  # type: ignore[method-assign]
    target = tmp_path / "out" / "e.json"
    target.parent.mkdir()
    errors: list[BaseException] = []

    def run() -> None:
        try:
            export_json(s, target)
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    del s.iter_facts
    assert errors == []
    ref = tmp_path / "ref.json"
    export_json(s, ref)
    assert target.read_bytes() == ref.read_bytes()  # a whole export, not two interleaved
    assert sorted(p.name for p in target.parent.iterdir()) == ["e.json"]  # no temp file left
    s.close()


def test_failed_export_leaves_no_temp_file(tmp_path: Path) -> None:
    s = SQLiteStore(tmp_path / "f.db")
    _fill(s, 5)

    def boom():  # type: ignore[no-untyped-def]
        raise RuntimeError("store went away")

    s.iter_tables = lambda **kw: boom()  # type: ignore[method-assign,assignment]
    target = tmp_path / "out" / "e.json"
    target.parent.mkdir()
    import pytest

    with pytest.raises(RuntimeError):
        export_json(s, target)
    assert list(target.parent.iterdir()) == []
    s.close()


def test_store_and_export_share_encoder(tmp_path: Path) -> None:
    import json
    from datetime import date
    from decimal import Decimal

    from schemaguru._util import hashing
    from schemaguru.store import export, postgres, sqlite

    assert sqlite._dumps is export._dumps is postgres._dumps is hashing.canonical_json
    s = SQLiteStore(tmp_path / "enc.db")
    f = Fact.make(
        FactKind.DESCRIPTION, ObjectRef(None, "shop", "orders"),
        {"text": "Bestellungen für Kunden ✓", "amount": Decimal("12.50"), "on": date(2026, 1, 2)},
        Provenance.HUMAN, Status.UNVERIFIED,
    )  # fmt: skip
    s.put_facts([f])
    (stored,) = s._q("SELECT data FROM facts")
    p = tmp_path / "e.json"
    export_json(s, p)
    (exported,) = json.loads(p.read_text("utf-8"))["facts"]
    # one encoder: the stored text is the exported text, byte for byte
    assert stored[0] == hashing.canonical_json(exported) == hashing.canonical_json(f.to_dict())
    assert (
        '"amount":"12.5"' in stored[0] and "für" in stored[0] and '"on":"2026-01-02"' in stored[0]
    )
    # and what is stored hashes like what was written (so a re-read fact keeps its id)
    assert Fact.from_dict(json.loads(stored[0])).id == f.id
    s.close()
