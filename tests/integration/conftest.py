"""Integration-test helpers: engine DSNs and the ``SCHEMAGURU_REQUIRE_ENGINES`` policy.

Locally a missing engine skips its tests. In CI ``SCHEMAGURU_REQUIRE_ENGINES`` lists the engines
that must be present: a missing DSN or driver is then a failure, never a silent skip.
"""

from __future__ import annotations

import importlib
import os

import pytest


def required_engines() -> frozenset[str]:
    raw = os.environ.get("SCHEMAGURU_REQUIRE_ENGINES", "")
    return frozenset(e.strip() for e in raw.split(",") if e.strip())


def dsn_var(engine: str) -> str:
    return f"SCHEMAGURU_TEST_{engine.upper()}_DSN"


def require_driver(engine: str, module: str) -> None:
    """Import ``module`` or skip (fail when the engine is required)."""
    try:
        importlib.import_module(module)
    except ImportError:
        msg = f"{engine}: driver module {module!r} is not installed"
        if engine in required_engines():
            pytest.fail(msg + " (required by SCHEMAGURU_REQUIRE_ENGINES)")
        pytest.skip(msg)


def engine_dsn(engine: str) -> str:
    """The engine's DSN, or skip (fail when the engine is required)."""
    dsn = os.environ.get(dsn_var(engine))
    if dsn:
        return dsn
    msg = f"{dsn_var(engine)} is not set"
    if engine in required_engines():
        pytest.fail(msg + " (required by SCHEMAGURU_REQUIRE_ENGINES)")
    pytest.skip(msg)
