"""A store written by SchemaGuru 0.1.0 still reads, verifies and renders the same.

``tests/fixtures/store_v010`` holds the golden shop knowledge as tag ``v0.1.0`` wrote it
(``build_v010.py``). Every test opens a fresh copy, so the fixture itself is never migrated.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from schemaguru.model.facts import Fact
from schemaguru.store.sqlite import SQLiteStore
from tests.golden.golden import VIEWS
from tests.golden.shop_knowledge import QUESTIONS
from tests.golden.test_golden_views import PROFILES, golden_view

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "store_v010"


def open_v010(tmp_path: Path) -> SQLiteStore:
    """A read-write :class:`SQLiteStore` on a fresh copy of the 0.1.0 fixture store."""
    copy = tmp_path / "v010" / "shop.db"
    copy.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FIXTURE / "shop.db", copy)
    return SQLiteStore(copy)


def _v010_facts() -> list[dict[str, object]]:
    facts: list[dict[str, object]] = json.loads((FIXTURE / "facts.json").read_text("utf-8"))
    return facts


def test_v010_fact_ids_still_verify(tmp_path: Path) -> None:
    expected = _v010_facts()
    assert expected  # negative control: the fixture holds facts
    with open_v010(tmp_path) as store:
        stored = sorted((f.to_dict() for f in store.iter_facts()), key=lambda d: str(d["id"]))
    assert stored == expected
    for d in expected:
        f = Fact.from_dict(d)
        again = Fact.make(
            f.kind,
            f.subject,
            f.value,
            f.provenance,
            f.status,
            method=f.method,
            evidence=f.evidence,
            source=f.source,
            observed_at=f.observed_at,
            snapshot_id=f.snapshot_id,
        )
        assert again.id == f.id == d["id"], d


@pytest.fixture(scope="module")
def v010_store(tmp_path_factory: pytest.TempPathFactory) -> Iterator[SQLiteStore]:
    with open_v010(tmp_path_factory.mktemp("v010")) as store:
        yield store


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("qi", range(len(QUESTIONS)))
def test_v010_golden_views_unchanged(v010_store: SQLiteStore, profile: str, qi: int) -> None:
    text = golden_view(v010_store, profile, QUESTIONS[qi])
    expected = (VIEWS / f"{profile}__q{qi + 1}.txt").read_bytes().decode("utf-8")
    assert text + "\n" == expected.replace("\r\n", "\n")


def test_v010_migrates_facts_and_views_unchanged(tmp_path: Path) -> None:
    with open_v010(tmp_path) as store:
        versions = [r[0] for r in store._q("SELECT version FROM schema_version ORDER BY version")]
        assert versions == [1, 2, 3]
        stored = sorted((f.to_dict() for f in store.iter_facts()), key=lambda d: str(d["id"]))
        assert stored == _v010_facts()
        assert store._q("SELECT COUNT(*) FROM lineage_edges") == [(0,)]
        assert store._q("SELECT COUNT(*) FROM lineage_state") == [(0,)]
        for profile in PROFILES:
            for qi, question in enumerate(QUESTIONS):
                text = golden_view(store, profile, question)
                expected = (VIEWS / f"{profile}__q{qi + 1}.txt").read_bytes().decode("utf-8")
                assert text + "\n" == expected.replace("\r\n", "\n"), (profile, qi)
