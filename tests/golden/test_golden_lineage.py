"""Golden lineage views: every built-in profile at STANDARD and LITE (0.2.0 spec 4.6).

Regenerate with ``SCHEMAGURU_UPDATE_GOLDEN=1 pytest tests/golden/test_golden_lineage.py`` and read
the files: lineage lines are parsed or declared, never measured or verified.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from schemaguru.model.detail import DetailLevel
from schemaguru.render import collect, get_profile
from schemaguru.store.sqlite import SQLiteStore
from tests.golden.golden import assert_golden
from tests.golden.lineage_knowledge import LINEAGE_TABLES, lineage_store
from tests.golden.shop_knowledge import CONFIG, NOW

PROFILES = ("agent", "planner", "verifier", "router")
LEVELS = {"standard": DetailLevel.STANDARD, "lite": DetailLevel.LITE}


@pytest.fixture(scope="module")
def store(tmp_path_factory: pytest.TempPathFactory) -> Iterator[SQLiteStore]:
    with lineage_store(tmp_path_factory.mktemp("lineage") / "shop.db") as s:
        yield s


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("level", sorted(LEVELS))
def test_golden_lineage(store: SQLiteStore, profile: str, level: str) -> None:
    p = get_profile(profile)
    text = "\n\n".join(
        f"--- {ref.canonical()}\n{p.render_table(collect(store, ref, NOW, CONFIG), LEVELS[level])}"
        for ref in LINEAGE_TABLES
    )
    assert_golden(f"lineage__{profile}__{level}.txt", text)
