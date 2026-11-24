"""Golden-file comparison. ``SCHEMAGURU_UPDATE_GOLDEN=1`` rewrites the files instead.

Files are compared with ``\\r\\n`` folded to ``\\n`` (a Windows checkout may convert line endings);
the rendered text itself never contains ``\\r``.
"""

from __future__ import annotations

import os
from pathlib import Path

VIEWS = Path(__file__).resolve().parent / "views"


def assert_golden(name: str, text: str) -> None:
    assert "\r" not in text
    path = VIEWS / name
    if os.environ.get("SCHEMAGURU_UPDATE_GOLDEN") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((text + "\n").encode("utf-8"))
        return
    assert path.exists(), f"missing golden file {path}; run with SCHEMAGURU_UPDATE_GOLDEN=1"
    expected = path.read_bytes().decode("utf-8").replace("\r\n", "\n")
    assert text + "\n" == expected, f"{name} differs from its golden file"
