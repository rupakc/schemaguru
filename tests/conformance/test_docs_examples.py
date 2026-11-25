"""Every code sample in the docs runs (spec 23.3).

* Each ```` ```python ```` block in ``docs/`` (the site; ``docs/wiki`` and ``docs/specs`` are
  not site pages) is executed. A page's blocks run in order in one namespace, like a reader
  typing them, in a fresh working directory holding the shop warehouse as
  ``warehouse.duckdb`` and the example overlay as ``schemaguru.yaml``.
* Each ``examples/*.py`` runs as ``__main__`` with that warehouse's path as its argument. An
  example that needs an optional framework says so in a ``# requires: <module>`` line and is
  skipped when the module is missing.
* ```` ```py ```` blocks are illustrations that need a real cloud warehouse, a network or an
  API key; they must still parse.

Measurement: the samples run on FactLang's ``Checker.measure`` (FactLang 0.1.2+). Should the
installed FactLang lack it, the default backend of every ``Guru`` is the test ``FakeBackend``
on the same DuckDB file, so the samples still show measured knowledge.
"""

from __future__ import annotations

import ast
import re
import runpy
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
EXAMPLES = ROOT / "examples"
NOT_SITE = ("wiki", "specs", "generated")
FENCE = re.compile(
    r"^(?P<indent>[ \t]*)```(?P<lang>[\w+-]*)[^\n]*\n(?P<body>.*?)^(?P=indent)```", re.M | re.S
)


def site_pages() -> list[Path]:
    return sorted(p for p in DOCS.rglob("*.md") if p.relative_to(DOCS).parts[0] not in NOT_SITE)


def blocks(text: str, lang: str) -> list[str]:
    out = []
    for m in FENCE.finditer(text):
        if m.group("lang") == lang:
            indent = len(m.group("indent"))
            out.append("\n".join(ln[indent:] for ln in m.group("body").splitlines()))
    return out


def requirements(source: str) -> list[str]:
    return [m.group(1) for m in re.finditer(r"^# requires: ([\w.]+)\s*$", source, re.M)]


@pytest.fixture(scope="session")
def shop_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    duckdb = pytest.importorskip("duckdb")
    from tests.fixtures.shop import build_shop

    path = tmp_path_factory.mktemp("docs-shop") / "warehouse.duckdb"
    conn = duckdb.connect(str(path))
    try:
        build_shop(conn, "duckdb")
    finally:
        conn.close()
    return path


@pytest.fixture
def workdir(tmp_path: Path, shop_db: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A working directory with the warehouse and overlay; measurement via FakeBackend when
    FactLang cannot measure."""
    import duckdb

    from schemaguru.guru import Guru
    from schemaguru.measure import flbridge
    from tests.unit.measure.fake_backend import FakeBackend

    shutil.copy(shop_db, tmp_path / "warehouse.duckdb")
    shutil.copy(EXAMPLES / "schemaguru.yaml", tmp_path / "schemaguru.yaml")
    monkeypatch.chdir(tmp_path)
    opened: list[Any] = []
    if "measure" not in flbridge.capabilities():
        conn = duckdb.connect(str(tmp_path / "warehouse.duckdb"), read_only=True)
        opened.append(conn)
        real = Guru._default_backend

        def default_backend(self: Guru, cfg: Any = None) -> Any:
            if self.connector is None and self.reader is None:
                return real(self, cfg)  # a store-only Guru measures nothing, as in real use
            return FakeBackend(conn)

        monkeypatch.setattr(Guru, "_default_backend", default_backend)
    yield tmp_path
    for c in opened:
        c.close()


REGISTRIES = (
    ("schemaguru.catalog.registry", "_FACTORIES"),
    ("schemaguru.model.kinds", "_REGISTRY"),
    ("schemaguru.pii.detectors", "_registry"),
    ("schemaguru.render.profiles", "_PROFILES"),
    ("schemaguru.render.formats", "_FORMATS"),
    ("schemaguru.refresh.classify", "_CUSTOM"),
    ("schemaguru.feedback.classifiers", "_PLUGINS"),
    ("schemaguru.measure.candidates", "_SOURCES"),
)


@pytest.fixture(autouse=True)
def restore_registries() -> Iterator[None]:
    """Extension pages register readers, kinds, detectors...: undo it after each page."""
    import importlib

    from schemaguru.catalog import dialects

    dialects()  # loads the built-in readers into the registry before it is copied
    saved = []
    for module, name in REGISTRIES:
        obj = getattr(importlib.import_module(module), name)
        saved.append((obj, obj.copy()))
    yield
    for obj, copy in saved:
        obj.clear()
        if isinstance(obj, dict):
            obj.update(copy)
        else:
            obj.extend(copy)


def _close_gurus(namespace: dict[str, Any]) -> None:
    from schemaguru.guru import Guru

    for value in list(namespace.values()):
        if isinstance(value, Guru):
            value.close()


@pytest.mark.parametrize("page", site_pages(), ids=lambda p: str(p.relative_to(DOCS)))
def test_python_blocks_run(page: Path, workdir: Path) -> None:
    namespace: dict[str, Any] = {"__name__": "__docs__"}
    try:
        for i, src in enumerate(blocks(page.read_text(encoding="utf-8"), "python")):
            try:
                exec(compile(src, f"{page.name}[block {i}]", "exec"), namespace)
            except Exception as e:
                pytest.fail(f"{page.relative_to(ROOT)} block {i} raised {e!r}:\n{src}")
    finally:
        _close_gurus(namespace)


def test_readme_python_blocks_run(workdir: Path) -> None:
    """The README's quickstart runs as written (on the shop warehouse as warehouse.duckdb)."""
    namespace: dict[str, Any] = {"__name__": "__readme__"}
    sources = blocks((ROOT / "README.md").read_text(encoding="utf-8"), "python")
    assert len(sources) >= 2  # the three-line quickstart and the measured follow-up
    assert sources[0].strip().splitlines() == [
        "import schemaguru as sg",
        'guru = sg.Guru.connect("duckdb:///warehouse.duckdb")',
        'print(guru.context("monthly revenue by customer segment"))',
    ]
    try:
        for i, src in enumerate(sources):
            try:
                exec(compile(src, f"README.md[block {i}]", "exec"), namespace)
            except Exception as e:
                pytest.fail(f"README.md block {i} raised {e!r}:\n{src}")
    finally:
        _close_gurus(namespace)


@pytest.mark.parametrize("page", site_pages(), ids=lambda p: str(p.relative_to(DOCS)))
def test_illustrations_parse(page: Path) -> None:
    for i, src in enumerate(blocks(page.read_text(encoding="utf-8"), "py")):
        try:
            ast.parse(src)
        except SyntaxError as e:
            pytest.fail(f"{page.relative_to(ROOT)} py block {i}: {e}")


EXAMPLE_FILES = sorted(p for p in EXAMPLES.glob("*.py") if not p.name.startswith("_"))


@pytest.mark.parametrize("example", EXAMPLE_FILES, ids=lambda p: p.name)
def test_example_runs(
    example: Path,
    workdir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for module in requirements(example.read_text(encoding="utf-8")):
        pytest.importorskip(module)
    monkeypatch.syspath_prepend(str(EXAMPLES))
    monkeypatch.setattr(sys, "argv", [str(example), str(workdir / "warehouse.duckdb")])
    runpy.run_path(str(example), run_name="__main__")
    out = capsys.readouterr().out
    assert out.strip(), f"{example.name} printed nothing"


def test_examples_markdown_parses() -> None:
    for page in EXAMPLES.glob("*.md"):
        for src in blocks(page.read_text(encoding="utf-8"), "py"):
            ast.parse(src)
        assert not blocks(page.read_text(encoding="utf-8"), "python"), page.name


def test_every_site_page_and_example_is_collected() -> None:
    assert len(site_pages()) >= 20
    assert {p.name for p in EXAMPLE_FILES} >= {
        "duckdb_quickstart.py",
        "langchain_agent.py",
        "anthropic_tools.py",
        "large_table_sampling.py",
    }


def test_negative_control_block_extraction() -> None:
    text = "x\n```python\na = 1\n```\n```py\nb = (\n```\n    ```python\n    c = 3\n    ```\n"
    assert blocks(text, "python") == ["a = 1", "c = 3"]
    assert blocks(text, "py") == ["b = ("]
    with pytest.raises(SyntaxError):
        ast.parse(blocks(text, "py")[0])
    assert requirements("# requires: langchain_core\nimport x\n") == ["langchain_core"]


def test_examples_run_without_the_repository_tests() -> None:
    """The examples need only the installed package and examples/: no module imports
    ``tests``, and the shop builder they ship is the test fixture, byte for byte."""
    for path in sorted(EXAMPLES.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            assert not [n for n in names if n == "tests" or n.startswith("tests.")], path.name
    copy = (EXAMPLES / "_shop_build.py").read_bytes()
    assert copy == (ROOT / "tests" / "fixtures" / "shop" / "build.py").read_bytes(), (
        "examples/_shop_build.py drifted from tests/fixtures/shop/build.py: copy it again"
    )


def test_examples_shop_builds_from_the_examples_dir_alone(tmp_path: Path) -> None:
    """Run _shop.shop_db() in an isolated interpreter (-I) whose path holds examples/ only:
    no repository root, no tests/ package."""
    pytest.importorskip("duckdb")
    import subprocess

    target = tmp_path / "examples"
    shutil.copytree(EXAMPLES, target, ignore=shutil.ignore_patterns("*.duckdb", "__pycache__"))
    code = "import _shop; p = _shop.shop_db([]); print(p.exists())"
    out = subprocess.run(
        [sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {str(target)!r}); {code}"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0 and out.stdout.strip() == "True", out.stderr


def test_pii_tokens_documented() -> None:  # ledger A1.17
    """The PII guide names every token masking can emit and says a hex token needs a letter."""
    from schemaguru.pii.mask import _TOKENS

    text = (DOCS / "guide" / "pii.md").read_text("utf-8")
    missing = [t for t in _TOKENS.values() if f"`{t}`" not in text]
    assert not missing, missing
    assert re.search(r"hex[^\n]*at least one letter", text), "hex-needs-a-letter rule"
