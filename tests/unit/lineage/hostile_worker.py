"""A parser worker that misbehaves on request (``python -m tests.unit.lineage.hostile_worker``).

It runs the real line loop (:func:`serve`) and the real handler, except for three magic
definitions: ``HANG`` sleeps 60 s, ``DIE`` exits at once, ``GARBAGE`` writes a non-JSON line.
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
    return handle(request)


if __name__ == "__main__":
    serve(_handler)
