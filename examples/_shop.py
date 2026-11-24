"""Find or build the example shop warehouse (a DuckDB file) for the examples.

The examples run offline against the shop fixture that SchemaGuru's own tests use: a generated
online-shop warehouse with known traps (a hub table, a TRUE/NULL soft-delete flag, a case-drifted
enum, a thin join, a broken join). Pass a path as the first argument to use an existing file;
otherwise ``warehouse.duckdb`` is built next to this file from a repository checkout.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def shop_db(argv: list[str] | None = None) -> Path:
    """The shop DuckDB file: ``argv[1]`` when given, else ``examples/warehouse.duckdb``."""
    args = sys.argv if argv is None else argv
    if len(args) > 1:
        return Path(args[1]).resolve()
    # the file name must not be the schema name: DuckDB names the catalog after the file
    path = HERE / "warehouse.duckdb"
    if not path.exists():
        import duckdb  # pip install 'schemaguru[duckdb]'

        sys.path.insert(0, str(ROOT))  # the fixture lives in the repository's tests/
        from tests.fixtures.shop import build_shop

        conn = duckdb.connect(str(path))
        try:
            build_shop(conn, "duckdb")
        finally:
            conn.close()
    return path


def url(path: Path) -> str:
    """``duckdb:///`` + the path: four slashes for an absolute POSIX path (SQLAlchemy style)."""
    return "duckdb:///" + path.as_posix()


def measured(report: object) -> bool:
    """Whether a measurement run measured anything; explains why not when it did not."""
    errors = getattr(report, "errors", [])
    if any(d.code == "SG304" for d in errors):
        print(
            "note: this FactLang build cannot measure yet (Checker.measure arrives with "
            "FactLang 0.1.0); the views below show catalog knowledge only.\n"
        )
        return False
    return True
