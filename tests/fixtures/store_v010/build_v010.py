"""Write the 0.1.0 fixture store: ``python3 build_v010.py OUTDIR``, run under tag ``v0.1.0``.

The store is the golden shop knowledge (``tests.golden.shop_knowledge.shop_store``) as SQLite
0.1.0 wrote it, checkpointed into one file. ``facts.json`` is every stored fact's ``to_dict()``
as 0.1.0 read it back, sorted by id. Later releases open a copy and must read it unchanged
(``tests/unit/store/test_v010_store.py``). Regenerate from the repository root with::

    git worktree add ../sg-v010 v0.1.0
    PYTHONPATH=../sg-v010/src:../sg-v010 python3 tests/fixtures/store_v010/build_v010.py \\
        tests/fixtures/store_v010
    git worktree remove --force ../sg-v010
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

from tests.golden.shop_knowledge import shop_store

from schemaguru import __version__


def build(outdir: Path) -> None:
    if __version__ != "0.1.0":
        raise SystemExit(f"run under tag v0.1.0, not SchemaGuru {__version__}")
    outdir.mkdir(parents=True, exist_ok=True)
    db = outdir / "shop.db"
    for stale in (db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
        stale.unlink(missing_ok=True)
    with shop_store(db) as store:
        facts = sorted((f.to_dict() for f in store.iter_facts()), key=lambda d: d["id"])
    conn = sqlite3.connect(db)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA journal_mode=DELETE")
    finally:
        conn.close()
    text = json.dumps(facts, sort_keys=True, indent=1, ensure_ascii=False) + "\n"
    (outdir / "facts.json").write_text(text, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: build_v010.py OUTDIR")
    build(Path(sys.argv[1]))
