from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from schemaguru.store.sqlite import SQLiteStore


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SQLiteStore]:
    s = SQLiteStore(tmp_path / "t.db")
    yield s
    s.close()
