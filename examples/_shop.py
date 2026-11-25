"""Find or build the example shop warehouse (a DuckDB file) for the examples.

The examples run offline against the shop warehouse that SchemaGuru's own tests use: a generated
online-shop warehouse with known traps (a hub table, a TRUE/NULL soft-delete flag, a case-drifted
enum, a thin join, a broken join). Pass a path as the first argument to use an existing file;
otherwise ``warehouse.duckdb`` is built next to this file by ``_shop_build.py`` (a copy of the
test fixture kept identical by a test), so the examples need only the installed package and
this directory -- no repository checkout.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def shop_db(argv: list[str] | None = None) -> Path:
    """The shop DuckDB file: ``argv[1]`` when given, else ``examples/warehouse.duckdb``."""
    args = sys.argv if argv is None else argv
    if len(args) > 1:
        return Path(args[1]).resolve()
    # the file name must not be the schema name: DuckDB names the catalog after the file
    path = HERE / "warehouse.duckdb"
    if not path.exists():
        import duckdb  # pip install 'schemaguru[duckdb]'
        from _shop_build import build_shop  # this directory, not the repository's tests/

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
            "note: the installed FactLang cannot measure (SchemaGuru needs FactLang 0.1.2 or "
            "later); the views below show catalog knowledge only.\n"
        )
        return False
    return True
