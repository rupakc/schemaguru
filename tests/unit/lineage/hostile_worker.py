"""A parser worker that misbehaves on request (``python -m tests.unit.lineage.hostile_worker``).

It runs the real line loop (:func:`serve`) and the real handler, except for magic definitions:
``HANG`` sleeps 60 s, ``DIE`` exits at once, ``GARBAGE`` writes a non-JSON line, ``ENDLESS``
writes one line that never ends, ``WRONGKIND`` answers an extract with a lineage reply. With the
argument ``noread`` it announces readiness and then never reads stdin.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any

from schemaguru.lineage.worker import handle, serve


def _handler(request: dict[str, Any]) -> dict[str, Any]:
    sql = request.get("sql")
    if sql == "HANG":
        time.sleep(60)
    elif sql == "DIE":
        os._exit(3)
    elif sql == "GARBAGE":
        sys.stdout.write("this is not json\n")
        sys.stdout.flush()
    elif sql == "ENDLESS":
        chunk = "x" * 65536
        while True:
            sys.stdout.write(chunk)
    elif sql == "WRONGKIND":
        return handle({"op": "lineage", "sql": "SELECT 1 FROM a", "dialect": "duckdb",
                       "schema": [], "columns": True})  # fmt: skip
    return handle(request)


if __name__ == "__main__":
    if sys.argv[1:] == ["noread"]:
        sys.stdout.write('{"ready": true}\n')
        sys.stdout.flush()
        time.sleep(60)
    else:
        serve(_handler)
