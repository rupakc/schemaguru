"""``SCHEMAGURU_REQUIRE_ENGINES`` turns a missing engine into a failure (CI), not a skip."""

from __future__ import annotations

import pytest

from tests.integration import conftest as c

pytestmark = pytest.mark.integration


def test_every_required_engine_has_a_dsn() -> None:
    missing = [e for e in sorted(c.required_engines()) if not c.os.environ.get(c.dsn_var(e))]
    assert not missing, f"required engines without a DSN: {missing}"


def test_missing_required_engine_fails_instead_of_skipping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative control for the policy itself."""
    monkeypatch.delenv("SCHEMAGURU_TEST_FOO_DSN", raising=False)
    monkeypatch.setenv("SCHEMAGURU_REQUIRE_ENGINES", "foo")
    with pytest.raises(pytest.fail.Exception):
        c.engine_dsn("foo")
    monkeypatch.setenv("SCHEMAGURU_REQUIRE_ENGINES", "bar")
    with pytest.raises(pytest.skip.Exception):
        c.engine_dsn("foo")
