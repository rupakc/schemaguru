"""Architecture rules (spec 5.1, global constraints).

* A module imports only from its own package or a strictly lower layer.
* Only the FactLang seam modules import ``factlang``.
* Third-party packages (other than ``factlang`` and ``yaml``) are imported lazily.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent.parent / "src" / "schemaguru"

# module prefix -> layer (spec 5.1). The longest matching prefix wins.
LAYERS: dict[str, int] = {
    "schemaguru.__about__": 0,
    "schemaguru.errors": 0,
    "schemaguru.diagnostics": 0,
    "schemaguru._util": 0,
    "schemaguru.plugins": 0,
    "schemaguru.model": 1,
    "schemaguru.store": 2,
    "schemaguru.config": 2,
    "schemaguru.catalog": 3,
    "schemaguru.pii": 3,
    "schemaguru.overlays": 3,
    "schemaguru.measure": 4,
    "schemaguru.knowledge": 4,
    "schemaguru.graph": 5,
    "schemaguru.draft": 5,
    "schemaguru.scope": 6,
    "schemaguru.render": 6,
    "schemaguru.refresh": 6,
    "schemaguru.feedback": 6,
    "schemaguru.guard": 6,
    "schemaguru.guru": 7,
    "schemaguru.agent": 7,
    "schemaguru.cli": 8,
    "schemaguru.__main__": 9,
    "schemaguru.server": 8,
}

# The only modules allowed to import ``factlang`` (at any level).
FACTLANG_SEAM = {
    "schemaguru.measure.flbridge",
    "schemaguru.catalog.executor",
    "schemaguru.render.fragments",
}

# Third-party top-level packages that may be imported at module level.
ALWAYS_AVAILABLE = {"factlang", "yaml", "schemaguru"}

# ``schemaguru/__init__.py`` is the public facade (spec 5.1): it re-exports from every layer.
FACADE = "schemaguru"


def module_name(path: Path, root: Path = SRC) -> str:
    rel = path.relative_to(root).with_suffix("")
    parts = ["schemaguru", *rel.parts]
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _py_files(root: Path = SRC) -> list[Path]:
    return sorted(root.rglob("*.py")) if root.exists() else []


def layer_key(module: str) -> str | None:
    """The LAYERS key (longest prefix) governing ``module``, or None."""
    best: str | None = None
    for key in LAYERS:
        if (module == key or module.startswith(key + ".")) and (
            best is None or len(key) > len(best)
        ):
            best = key
    return best


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _module_level_imports(
    tree: ast.AST, skip_type_checking: bool = False
) -> list[ast.Import | ast.ImportFrom]:
    """Imports outside function bodies (module level, incl. if/try).

    With ``skip_type_checking`` the body of ``if TYPE_CHECKING:`` is ignored (it never runs),
    but its ``else:`` branch is still visited.
    """
    found: list[ast.Import | ast.ImportFrom] = []

    def visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
                continue
            if skip_type_checking and isinstance(child, ast.If) and _is_type_checking(child.test):
                for stmt in child.orelse:
                    if isinstance(stmt, ast.Import | ast.ImportFrom):
                        found.append(stmt)
                    visit(stmt)
                continue
            if isinstance(child, ast.Import | ast.ImportFrom):
                found.append(child)
            visit(child)

    visit(tree)
    return found


def _all_imports(tree: ast.AST) -> list[ast.Import | ast.ImportFrom]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.Import | ast.ImportFrom)]


def _top_levels(node: ast.Import | ast.ImportFrom) -> list[str]:
    """Absolute top-level package names imported by ``node`` (relative imports: none)."""
    if isinstance(node, ast.Import):
        return [a.name.split(".")[0] for a in node.names]
    if node.level > 0 or node.module is None:
        return []
    return [node.module.split(".")[0]]


def _imported_modules(node: ast.Import | ast.ImportFrom, importer: str, is_pkg: bool) -> list[str]:
    """Absolute ``schemaguru.*`` module names imported by ``node``."""
    if isinstance(node, ast.Import):
        return [a.name for a in node.names if a.name.split(".")[0] == "schemaguru"]
    if node.level > 0:
        pkg = importer.split(".") if is_pkg else importer.split(".")[:-1]
        base = pkg[: len(pkg) - (node.level - 1)]
        mod = ".".join([*base, *(node.module.split(".") if node.module else [])])
    else:
        mod = node.module or ""
    if mod.split(".")[0] != "schemaguru":
        return []
    out: list[str] = []
    for alias in node.names:
        cand = f"{mod}.{alias.name}"
        out.append(cand if cand in LAYERS else mod)
    return out or [mod]


def check_layering(
    source: str, importer: str, filename: str = "<module>", is_pkg: bool = False
) -> list[str]:
    """Violations: module-level imports not from the own package or a strictly lower layer."""
    if importer == FACADE:
        return []
    own_key = layer_key(importer)
    if own_key is None:
        return [f"{filename}: module '{importer}' has no layer in LAYERS"]
    own = LAYERS[own_key]
    problems: list[str] = []
    # TYPE_CHECKING imports count: the layer rule holds at the type level too.
    for node in _module_level_imports(ast.parse(source, filename)):
        for target in _imported_modules(node, importer, is_pkg):
            key = layer_key(target)
            if target == FACADE:
                problems.append(
                    f"{filename}:{node.lineno}: '{importer}' imports the public facade "
                    f"'{FACADE}'; import the defining module"
                )
            elif key is None:
                problems.append(f"{filename}:{node.lineno}: '{target}' has no layer in LAYERS")
            elif key != own_key and LAYERS[key] >= own:
                problems.append(
                    f"{filename}:{node.lineno}: '{importer}' (layer {own}) imports "
                    f"'{target}' (layer {LAYERS[key]})"
                )
    return problems


def check_factlang_seam(source: str, importer: str, filename: str = "<module>") -> list[str]:
    """Violations: ``factlang`` imported (at any level) outside the seam modules."""
    if importer in FACTLANG_SEAM:
        return []
    return [
        f"{filename}:{node.lineno}: '{importer}' imports factlang outside the seam"
        for node in _all_imports(ast.parse(source, filename))
        if "factlang" in _top_levels(node)
    ]


def check_optional_deps_lazy(source: str, filename: str = "<module>") -> list[str]:
    """Violations: module-level third-party imports other than factlang/yaml."""
    allowed = set(sys.stdlib_module_names) | ALWAYS_AVAILABLE
    return [
        f"{filename}:{node.lineno}: module-level third-party import '{top}'"
        for node in _module_level_imports(ast.parse(source, filename), skip_type_checking=True)
        for top in _top_levels(node)
        if top not in allowed
    ]


# --- the real tree ---------------------------------------------------------------


def check_tree_layering(root: Path = SRC) -> list[str]:
    """The real-tree walker: every module under ``root`` through :func:`check_layering`."""
    files = _py_files(root)
    if not files:
        return [f"no source files found under {root}"]
    problems: list[str] = []
    for path in files:
        problems += check_layering(
            path.read_text(encoding="utf-8"),
            module_name(path, root),
            str(path),
            is_pkg=path.name == "__init__.py",
        )
    return problems


def test_layering() -> None:
    problems = check_tree_layering()
    assert not problems, "\n".join(problems)


def test_factlang_seam() -> None:
    assert _py_files(), f"no source files found under {SRC}"
    problems: list[str] = []
    for path in _py_files():
        problems += check_factlang_seam(
            path.read_text(encoding="utf-8"), module_name(path), str(path)
        )
    assert not problems, "\n".join(problems)


# FactLang's public modules (its SemVer-covered API); anything deeper is private to FactLang.
FACTLANG_PUBLIC = {
    "factlang",
    "factlang.ir",
    "factlang.sql",
    "factlang.connectors",
    "factlang.agent",
    "factlang.syntax",
}
_FACTLANG_MODULE = re.compile(r"factlang(?:\.[A-Za-z_]\w*)*")


def _is_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def check_factlang_public(source: str, filename: str = "<module>") -> list[str]:
    """Violations: a FactLang module outside ``FACTLANG_PUBLIC``, imported by statement (also as
    a name, ``from factlang import semantics``) or named as a string
    (``importlib.import_module("factlang.x")``, ``_attr("factlang.x", ...)``)."""
    tree = ast.parse(source, filename)
    named: list[tuple[int, str]] = []
    for node in _all_imports(tree):
        if isinstance(node, ast.Import):
            named += [(node.lineno, a.name) for a in node.names]
        elif node.level == 0 and node.module is not None:
            named.append((node.lineno, node.module))
            if node.module.split(".")[0] == "factlang":
                # an imported name that is itself a FactLang module counts as that module
                named += [
                    (node.lineno, f"{node.module}.{a.name}")
                    for a in node.names
                    if _is_module(f"{node.module}.{a.name}")
                ]
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and _FACTLANG_MODULE.fullmatch(node.value)
        ):
            named.append((node.lineno, node.value))
    return [
        f"{filename}:{line}: '{mod}' is not a public FactLang module"
        for line, mod in named
        if mod.split(".")[0] == "factlang" and mod not in FACTLANG_PUBLIC
    ]


def test_factlang_public_modules_only() -> None:
    assert _py_files(), f"no source files found under {SRC}"
    problems: list[str] = []
    for path in _py_files():
        problems += check_factlang_public(path.read_text(encoding="utf-8"), str(path))
    assert not problems, "\n".join(problems)


def test_negative_control_factlang_public_modules_only() -> None:
    for bad in (
        "from factlang.semantics.refs import Ident\n",
        "import factlang.sql.probe\n",
        "import importlib\nm = importlib.import_module('factlang.semantics.refs')\n",
        "def f():\n    return _attr('factlang.connectors.base', 'X')\n",
        "from factlang import semantics\nIdent = semantics.refs.Ident\n",  # a private submodule
        "from factlang.sql import probe\n",
    ):
        assert len(check_factlang_public(bad)) == 1, bad
    ok = "from factlang.sql import Param\nimport factlang\nx = 'factlang.ir'\ny = 'a factlang.x'\n"
    assert check_factlang_public(ok) == []
    names = "from factlang import Checker, connectors\nfrom factlang.syntax import FILTER_KEYWORDS"
    assert check_factlang_public(names) == []


def test_optional_deps_lazy() -> None:
    assert _py_files(), f"no source files found under {SRC}"
    problems: list[str] = []
    for path in _py_files():
        problems += check_optional_deps_lazy(path.read_text(encoding="utf-8"), str(path))
    assert not problems, "\n".join(problems)


# --- negative controls: each checker must flag a violating temp module ------------


def _write(tmp_path: Path, name: str, text: str) -> tuple[str, str]:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p.read_text(encoding="utf-8"), str(p)


def test_negative_control_layering(tmp_path: Path) -> None:
    src, fn = _write(tmp_path, "up.py", "from schemaguru.graph import search\n")
    problems = check_layering(src, "schemaguru.model.ref", fn)
    assert len(problems) == 1 and "layer 5" in problems[0]
    # a same-layer sibling package is also a violation
    src, fn = _write(tmp_path, "side.py", "import schemaguru.pii.detect\n")
    assert check_layering(src, "schemaguru.catalog.crawl", fn)
    # an unmapped module is reported, not ignored
    assert check_layering("x = 1\n", "schemaguru.mystery", "m.py")


def test_layering_allows_downward_same_package_and_lazy_upward() -> None:
    ok = "from schemaguru.model import Fact\nfrom .sibling import x\nfrom ..errors import E\n"
    assert check_layering(ok, "schemaguru.store.sqlite") == []
    assert check_layering("from schemaguru.catalog import x\n", "schemaguru.catalog.crawl") == []
    lazy = "def f():\n    from schemaguru.guru import Guru\n"
    assert check_layering(lazy, "schemaguru.model.ref") == []


def test_facade_is_exempt_but_others_cannot_import_it() -> None:
    assert check_layering("from schemaguru.guru import Guru\n", "schemaguru", is_pkg=True) == []
    # `from schemaguru import X` (the facade) is layer-unknown for any real module
    assert check_layering("from schemaguru import Guru\n", "schemaguru.model.ref")


def test_negative_control_factlang_seam(tmp_path: Path) -> None:
    src, fn = _write(tmp_path, "m.py", "import factlang\n")
    assert len(check_factlang_seam(src, "schemaguru.measure.planner", fn)) == 1
    # lazy imports count too: the seam rule holds at any level
    src, fn = _write(tmp_path, "l.py", "def f():\n    from factlang.sql import render_join\n")
    assert len(check_factlang_seam(src, "schemaguru.graph.edges", fn)) == 1
    # seam modules may import it
    for mod in FACTLANG_SEAM:
        assert check_factlang_seam("import factlang\n", mod) == []


def test_negative_control_optional_deps_lazy(tmp_path: Path) -> None:
    src, fn = _write(tmp_path, "d.py", "import duckdb\nfrom numpy import array\nimport yaml\n")
    problems = check_optional_deps_lazy(src, fn)
    assert len(problems) == 2
    assert any("duckdb" in p for p in problems) and any("numpy" in p for p in problems)


def test_optional_deps_lazy_allows_stdlib_factlang_yaml_and_lazy() -> None:
    src = (
        "import json\nimport yaml\nimport factlang\nfrom schemaguru.model import X\n"
        "def f():\n    import duckdb\n"
    )
    assert check_optional_deps_lazy(src) == []
    guarded = "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import numpy\n"
    assert check_optional_deps_lazy(guarded) == []


def test_layer_table_matches_spec_5_1() -> None:
    assert LAYERS["schemaguru.errors"] == 0
    assert LAYERS["schemaguru.model"] == 1
    assert LAYERS["schemaguru.store"] == LAYERS["schemaguru.config"] == 2
    assert LAYERS["schemaguru.measure"] == LAYERS["schemaguru.knowledge"] == 4
    assert LAYERS["schemaguru.server"] == 8
    assert {
        "schemaguru.measure.flbridge",
        "schemaguru.catalog.executor",
        "schemaguru.render.fragments",
    } == FACTLANG_SEAM
    # every seam module is governed by a layer
    assert all(layer_key(m) for m in FACTLANG_SEAM)


def test_negative_control_type_checking_upward_import(tmp_path: Path) -> None:
    body = (
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from schemaguru.guru import G\n"
    )
    src, fn = _write(tmp_path, "tc.py", body)
    assert check_layering(src, "schemaguru.model.ref", fn)  # flagged by the layer check
    assert check_optional_deps_lazy(src, fn) == []  # not by the lazy-deps check
    # the else branch is still checked by both rules
    other = (
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    pass\nelse:\n"
        "    import numpy\n    from schemaguru.guru import G\n"
    )
    src, fn = _write(tmp_path, "tc2.py", other)
    assert check_layering(src, "schemaguru.model.ref", fn)
    assert len(check_optional_deps_lazy(src, fn)) == 1
    # a downward TYPE_CHECKING import is fine
    ok = (
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from schemaguru.errors import E\n"
    )
    assert check_layering(ok, "schemaguru.model.ref") == []


def test_real_tree_guard_detects_missing_source(tmp_path: Path) -> None:
    assert _py_files(tmp_path / "does-not-exist") == []
    assert _py_files()


def _tmp_tree(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "schemaguru"
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


def test_walker_flags_tmp_tree(tmp_path: Path) -> None:
    root = _tmp_tree(
        tmp_path,
        {
            "model/__init__.py": "",
            "model/bad.py": "from schemaguru.guru import Guru\n",
            "guru/__init__.py": "",
            "guru/ok.py": "from schemaguru.model import bad\n",
        },
    )
    problems = check_tree_layering(root)
    assert len(problems) == 1 and "bad.py" in problems[0] and "layer 7" in problems[0]
    # the walker also wires `__init__.py` as a package (relative imports resolve from it)
    root = _tmp_tree(tmp_path / "rel", {"store/__init__.py": "from ..guru import G\n"})
    assert len(check_tree_layering(root)) == 1


def test_facade_import_message() -> None:
    (msg,) = check_layering("from schemaguru import Guru\n", "schemaguru.model.ref", "m.py")
    assert "imports the public facade 'schemaguru'; import the defining module" in msg
    assert "no layer" not in msg
    (msg,) = check_layering("import schemaguru\n", "schemaguru.model.ref", "m.py")
    assert "public facade" in msg


def test_nested_type_checking_real_tree(tmp_path: Path) -> None:
    nested = (
        "import sys\nfrom typing import TYPE_CHECKING\n"
        "if sys.version_info >= (3, 11):\n    if TYPE_CHECKING:\n"
        "        from schemaguru.guru import G\n        import numpy\n"
    )
    root = _tmp_tree(tmp_path, {"model/__init__.py": "", "model/n.py": nested})
    # the layer rule still holds under a nested TYPE_CHECKING; the lazy-deps rule skips it
    assert len(check_tree_layering(root)) == 1
    assert check_optional_deps_lazy(nested, "n.py") == []
    # ... and the else branch of a nested guard stays subject to both
    other = nested + "    else:\n        import numpy\n"
    assert len(check_optional_deps_lazy(other, "n.py")) == 1
    # the I2 guard: an empty or missing tree is itself a finding, never a silent pass
    assert check_tree_layering(tmp_path / "does-not-exist")
    (tmp_path / "empty").mkdir()
    assert "no source files" in check_tree_layering(tmp_path / "empty")[0]
    assert check_tree_layering() == []  # and the real tree is clean
