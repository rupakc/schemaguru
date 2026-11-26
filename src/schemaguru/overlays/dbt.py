"""Import a dbt ``manifest.json`` as DECLARED, UNVERIFIED facts.

What is imported, only for relations and columns already present in the store:

* model/source/seed/snapshot and column descriptions -> ``DESCRIPTION``;
* ``relationships`` tests -> ``JOIN`` (cardinality unknown: dbt asserts only that child values
  exist in the parent);
* ``accepted_values`` tests -> ``VALUES``;
* a table's single ``unique`` test, on a column that also has ``not_null`` -> ``PRIMARY_KEY``
  (every ``unique`` test on the table counts, even one whose column cannot be mapped).

Everything is ``DECLARED`` and ``UNVERIFIED``: dbt states intent, it measures nothing here, so
the numbers a measurement produces come only from measuring. An unreadable or hostile manifest
is reported as ``SG209`` and yields no facts; it never raises.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from schemaguru.diagnostics import Diagnostic, DiagnosticBag, Severity
from schemaguru.model.facts import Fact, Provenance, Status
from schemaguru.model.joins import JoinRef
from schemaguru.model.kinds import FactKind
from schemaguru.model.refs import ObjectRef
from schemaguru.store.protocol import Store

_RELATIONS = ("model", "seed", "snapshot", "source")
_REF = re.compile(r"""(?:ref|source)\(\s*(['"])(.+?)\1(?:\s*,\s*(['"])(.+?)\3)?\s*\)""")
SOURCE = "dbt:manifest"


def _list(x: Any) -> list[Any]:
    return x if isinstance(x, list) else []


def _dict(x: Any) -> dict[str, Any]:
    return x if isinstance(x, dict) else {}


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


class _Importer:
    def __init__(self, manifest: dict[str, Any], store: Store, bag: DiagnosticBag) -> None:
        self.store = store
        self.bag = bag
        self.nodes: dict[str, dict[str, Any]] = {}
        for section in ("nodes", "sources"):
            for uid, node in _dict(manifest.get(section)).items():
                if isinstance(node, dict):
                    self.nodes[str(uid)] = node
        self.facts: list[Fact] = []
        self._tables: dict[str, ObjectRef | None] = {}

    def table_of(self, uid: str) -> ObjectRef | None:
        if uid in self._tables:
            return self._tables[uid]
        node = self.nodes.get(uid)
        found: ObjectRef | None = None
        if node is not None and node.get("resource_type") in _RELATIONS:
            name = node.get("identifier") or node.get("alias") or node.get("name")
            parts = [node.get("database"), node.get("schema"), name]
            for cand in (parts, parts[1:]):
                strs = [p for p in cand if isinstance(p, str) and p]
                if len(strs) == len(cand):
                    text = ".".join(ObjectRef(None, None, p).canonical() for p in strs)
                    ref, _ = self.store.resolve(text)
                    if ref is not None and ref.column is None:
                        found = ref
                        break
        self._tables[uid] = found
        return found

    def column_of(self, table: ObjectRef, name: str) -> ObjectRef | None:
        names = [c.ref.column for c in self.store.columns(table) if c.ref.column]
        hit = [n for n in names if n == name] or [n for n in names if n.lower() == name.lower()]
        if len(hit) != 1:
            return None
        return ObjectRef(table.catalog, table.schema, table.table, hit[0])

    def add(self, kind: Any, subject: ObjectRef | JoinRef, value: dict[str, Any], uid: str) -> None:
        self.facts.append(
            Fact.make(
                kind, subject, value, Provenance.DECLARED, Status.UNVERIFIED,
                source=f"{SOURCE}:{uid}",
            )
        )  # fmt: skip

    def descriptions(self) -> None:
        for uid, node in self.nodes.items():
            table = self.table_of(uid)
            if table is None:
                continue
            desc = node.get("description")
            if isinstance(desc, str) and desc.strip():
                self.add(FactKind.DESCRIPTION, table, {"text": desc.strip()}, uid)
            cols = node.get("columns")
            for cname, col in cols.items() if isinstance(cols, dict) else []:
                text = col.get("description") if isinstance(col, dict) else None
                if isinstance(text, str) and text.strip():
                    cref = self.column_of(table, str(cname))
                    if cref is not None:
                        self.add(FactKind.DESCRIPTION, cref, {"text": text.strip()}, uid)

    def parent_table(self, kwarg_to: Any, test: dict[str, Any]) -> ObjectRef | None:
        """A relationships test's ``to`` (``ref()``/``source()``), resolved via depends_on."""
        attached = test.get("attached_node")
        deps = [d for d in _list(_dict(test.get("depends_on")).get("nodes")) if d != attached]
        if isinstance(kwarg_to, str):
            m = _REF.search(kwarg_to)
            if m:
                want = m.group(4) or m.group(2)  # source('s','t') -> t ; ref('m') -> m
                for d in deps:
                    n = self.nodes.get(d)
                    if n is not None and want in (n.get("name"), n.get("identifier")):
                        return self.table_of(d)
        if len(deps) == 1:
            return self.table_of(deps[0])
        return None

    def tests(self) -> None:
        uniques: dict[ObjectRef, list[tuple[ObjectRef, str]]] = {}
        unique_tests: dict[ObjectRef, int] = {}  # every unique test, resolvable or not
        not_null: set[ObjectRef] = set()
        for uid, test in self.nodes.items():
            if test.get("resource_type") != "test":
                continue
            meta = _dict(test.get("test_metadata"))
            name, kw = meta.get("name"), _dict(meta.get("kwargs"))
            attached = test.get("attached_node")
            col_name = kw.get("column_name") or test.get("column_name")
            if not isinstance(attached, str):
                continue
            table = self.table_of(attached)
            if name == "unique" and table is not None:
                unique_tests[table] = unique_tests.get(table, 0) + 1
            if not isinstance(col_name, str):
                continue
            col = None if table is None else self.column_of(table, col_name)
            if table is None or col is None:
                continue
            if name == "relationships":
                parent = self.parent_table(kw.get("to"), test)
                field = kw.get("field")
                if parent is None or not isinstance(field, str):
                    continue
                pcol = self.column_of(parent, field)
                if pcol is not None:
                    # dbt asserts only that child values exist in the parent: cardinality unknown
                    self.add(FactKind.JOIN, JoinRef((col,), (pcol,)), {"cardinality": None}, uid)
            elif name == "accepted_values":
                vals = kw.get("values")
                if isinstance(vals, list) and vals:
                    self.add(
                        FactKind.VALUES,
                        col,
                        {"values": _unique([str(v) for v in vals]), "case_sensitive": True,
                         "split": None},
                        uid,
                    )  # fmt: skip
            elif name == "unique":
                uniques.setdefault(table, []).append((col, uid))
            elif name == "not_null":
                not_null.add(col)
        # `unique` alone is not a key; it is PRIMARY_KEY only as the table's single unique test
        # on a column that is also not_null. (not_null itself is not imported.)
        for table, found in uniques.items():
            if len(found) == 1 and unique_tests.get(table) == 1 and found[0][0] in not_null:
                col, uid = found[0]
                self.add(FactKind.PRIMARY_KEY, table, {"columns": [col.column]}, uid)


def import_dbt(
    manifest_path: str | Path, store: Store, *, diagnostics: DiagnosticBag | None = None
) -> list[Fact]:
    """Facts from a dbt manifest; only relations and columns present in ``store`` are mapped.

    Descriptions become DECLARED ``DESCRIPTION``; ``relationships`` -> ``JOIN``,
    ``accepted_values`` -> ``VALUES``; ``unique`` -> ``PRIMARY_KEY`` only as a table's single
    ``unique`` test on a column that also has ``not_null``; ``not_null`` itself is not imported.
    All are DECLARED and UNVERIFIED. An unreadable or malformed manifest is reported as
    ``SG209`` on ``diagnostics`` (if given) and yields no facts; it never raises.
    """
    bag = diagnostics if diagnostics is not None else DiagnosticBag()
    path = Path(manifest_path)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        bag.add(Diagnostic("SG209", Severity.ERROR, f"cannot read dbt manifest: {e}", str(path)))
        return []
    if not isinstance(manifest, dict):
        bag.add(Diagnostic("SG209", Severity.ERROR, "dbt manifest is not a JSON object", str(path)))
        return []
    try:
        imp = _Importer(manifest, store, bag)
        imp.descriptions()
        imp.tests()
    except Exception as e:  # a hostile manifest must never sink the caller
        bag.add(
            Diagnostic("SG209", Severity.ERROR, f"cannot interpret dbt manifest: {e}", str(path))
        )
        return []
    return imp.facts
