"""Import a dbt ``manifest.json`` as DECLARED, UNVERIFIED facts.

What is imported, only for relations and columns already present in the store:

* model/source/seed/snapshot and column descriptions -> ``DESCRIPTION``;
* ``relationships`` tests -> ``JOIN`` (cardinality unknown: dbt asserts only that child values
  exist in the parent);
* ``accepted_values`` tests -> ``VALUES``;
* a table's single ``unique`` test, on a column that also has ``not_null`` -> ``PRIMARY_KEY``
  (every ``unique`` test on the table counts, even one whose column cannot be mapped).

* lineage (0.2.0 spec 4.2): a ``LINEAGE`` per model/snapshot/seed from ``depends_on.nodes``
  (an ephemeral model's inputs pass to its consumers), and a ``COLUMN_LINEAGE`` per column
  parsed from ``compiled_code`` when the ``lineage`` extra is installed. ``compiled_code`` is raw
  SQL: it stays in memory; only facts, its sha256 and the literal-free template are kept.

Everything is ``DECLARED`` and ``UNVERIFIED``: dbt states intent, it measures nothing here, so
the numbers a measurement produces come only from measuring. An unreadable or hostile manifest
is reported as ``SG209`` and yields no facts; it never raises.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from schemaguru.diagnostics import Diagnostic, DiagnosticBag, Severity
from schemaguru.lineage.analyse import (
    DefinitionItem,
    DefinitionResult,
    Resolver,
    analyse_definitions,
    definition_sha256,
    extra_missing_diagnostic,
    failure_diagnostic,
    lineage_facts,
)
from schemaguru.lineage.dialects import DBT_ADAPTERS
from schemaguru.lineage.parse import lineage_available
from schemaguru.lineage.worker import ParserPool
from schemaguru.model.evidence import Evidence
from schemaguru.model.facts import Fact, Provenance, Status
from schemaguru.model.joins import JoinRef
from schemaguru.model.kinds import LINEAGE_KINDS, FactKind
from schemaguru.model.lineage import (
    RawName,
    ResolvedTable,
    table_lineage_value,
)
from schemaguru.model.refs import ObjectRef
from schemaguru.store.names import resolve_relation_names
from schemaguru.store.protocol import Store

_RELATIONS = ("model", "seed", "snapshot", "source")
_TARGETS = ("model", "seed", "snapshot")
DBT_LINEAGE_STATE = "dbt_lineage_facts"
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
        self.manifest = manifest
        self.shas: dict[str, str] = {}
        self._lineage: dict[str, tuple[str, Fact]] = {}
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

    # ----- lineage ----------------------------------------------------------------------------

    @staticmethod
    def _ephemeral(node: dict[str, Any]) -> bool:
        return _dict(node.get("config")).get("materialized") == "ephemeral"

    @staticmethod
    def _sql(node: dict[str, Any]) -> str | None:
        sql = node.get("compiled_code")
        return sql if isinstance(sql, str) and sql.strip() else None

    def _name_of(self, node: dict[str, Any]) -> str:
        name = node.get("identifier") or node.get("alias") or node.get("name")
        parts = (node.get("database"), node.get("schema"), name)
        return ".".join(p for p in parts if isinstance(p, str) and p)

    def _inputs(self, uid: str) -> tuple[list[str], list[str]]:
        """The relations ``uid`` reads: ``depends_on.nodes`` that are relations, an ephemeral
        model replaced by its own inputs (transitively, once each)."""
        upstream: set[str] = set()
        unresolved: set[str] = set()
        seen: set[str] = {uid}
        stack = [str(d) for d in _list(_dict(self.nodes[uid].get("depends_on")).get("nodes"))]
        while stack:
            dep = stack.pop()
            node = self.nodes.get(dep)
            if dep in seen or node is None or node.get("resource_type") not in _RELATIONS:
                continue
            seen.add(dep)
            if self._ephemeral(node):
                stack.extend(str(d) for d in _list(_dict(node.get("depends_on")).get("nodes")))
                continue
            table = self.table_of(dep)
            if table is not None:
                upstream.add(table.canonical())
            elif name := self._name_of(node):
                unresolved.add(name)
        return sorted(upstream), sorted(unresolved)

    def _resolved(self, ref: ObjectRef) -> ResolvedTable:
        cols = sorted(self.store.columns(ref), key=lambda c: c.ordinal)
        return ResolvedTable(ref.canonical(), tuple(c.ref.column or "" for c in cols))

    def _resolver(self) -> Resolver:
        """Names in compiled SQL: the store's own resolution first, then the manifest's
        database.schema.name of a relation in the store (the store may hold no catalog)."""
        index: list[tuple[tuple[str, str, str], ObjectRef]] = []
        for uid, node in self.nodes.items():
            table = self.table_of(uid)
            if table is not None:
                parts = (node.get("database"), node.get("schema"))
                name = node.get("identifier") or node.get("alias") or node.get("name")
                key = tuple(str(p).casefold() if isinstance(p, str) else "" for p in (*parts, name))
                index.append(((key[0], key[1], key[2]), table))

        def resolve(names: Sequence[RawName], default: ObjectRef) -> dict[RawName, ResolvedTable]:
            out = dict(resolve_relation_names(self.store, names, default))
            for name in names:
                if name in out:
                    continue
                c, s, n = (None if x is None else x.casefold() for x in name)
                hits = {
                    t
                    for (kc, ks, kn), t in index
                    if kn == n and (s is None or ks == s) and (c is None or kc == c)
                }
                if len(hits) == 1:
                    out[name] = self._resolved(next(iter(hits)))
            return out

        return resolve

    def _analyse(self, targets: list[str], pool: ParserPool | None) -> dict[str, DefinitionResult]:
        """Parse each target's ``compiled_code`` (the SQL lives only in these local items)."""
        adapter = _dict(self.manifest.get("metadata")).get("adapter_type")
        dialect = DBT_ADAPTERS.get(adapter, adapter) if isinstance(adapter, str) else ""
        order: list[str] = []
        items: list[DefinitionItem] = []
        for uid in targets:
            sql = self._sql(self.nodes[uid])
            table = self.table_of(uid)
            if sql is not None and table is not None:
                order.append(uid)
                items.append(DefinitionItem(table, sql, dialect))
        if not items:
            return {}
        if not lineage_available():
            self.bag.add(extra_missing_diagnostic())
            return {}
        from schemaguru.pii.mask import definition_template  # pii is not below overlays

        own = pool is None
        active = ParserPool() if pool is None else pool
        try:
            try:
                results = analyse_definitions(
                    items, resolve=self._resolver(), pool=active, template=definition_template
                )
            except Exception:  # the pool broke its contract; its message is not kept
                results = [
                    DefinitionResult(i.target, i.dialect, definition_sha256(i.sql), None,
                                     "worker_died", None)
                    for i in items
                ]  # fmt: skip
        finally:
            items.clear()  # the raw texts end here
            if own:
                active.close()
        return dict(zip(order, results, strict=True))

    def _keep(self, uid: str, fact: Fact) -> None:
        """One fact per id, decided by node id, never by batch order. A different payload on
        the same id (two nodes write one relation with equal inputs but another transform) is
        reported."""
        first = self._lineage.get(fact.id)
        if first is None:
            self._lineage[fact.id] = (uid, fact)
        elif first[1].value != fact.value:
            where = fact.subject.canonical()
            self.bag.add(
                Diagnostic(
                    "SG215",
                    Severity.WARNING,
                    f"dbt nodes {first[0]} and {uid} both write {where} with the same inputs but "
                    f"different details; kept {first[0]}",
                    where,
                )
            )

    def lineage(self, pool: ParserPool | None) -> None:
        targets = sorted(
            uid
            for uid, n in self.nodes.items()
            if n.get("resource_type") in _TARGETS and not self._ephemeral(n)
        )
        nodes = self.nodes.values()
        has_models = any(n.get("resource_type") in _TARGETS for n in nodes)
        if has_models and not any(self._sql(n) for n in nodes):
            self.bag.add(
                Diagnostic(
                    "SG214",
                    Severity.INFO,
                    "the dbt manifest has no compiled_code (run `dbt compile`): only "
                    "table-level lineage was imported",
                )
            )
        for uid in targets:  # stdlib hashing: known with or without the extra
            sql = self._sql(self.nodes[uid])
            if sql is not None and self.table_of(uid) is not None:
                self.shas[uid] = definition_sha256(sql)
        results = self._analyse(targets, pool)
        for uid in targets:
            table = self.table_of(uid)
            if table is None:
                continue
            source = f"{SOURCE}:{uid}"
            res = results.get(uid)
            upstream, unresolved = self._inputs(uid)
            if upstream or unresolved:
                ev = (
                    Evidence(sql=res.template, dialect=res.dialect)
                    if res is not None and res.template is not None
                    else None
                )
                self._keep(
                    uid,
                    Fact.make(
                        FactKind.LINEAGE, table, table_lineage_value(upstream, unresolved),
                        Provenance.DECLARED, Status.UNVERIFIED, evidence=ev, source=source,
                    ),
                )  # fmt: skip
            if res is None:
                continue
            if res.parsed is None:
                self.bag.add(failure_diagnostic(table, res.failure or "parse_error"))
                continue
            cols = sorted(self.store.columns(table), key=lambda c: c.ordinal)
            for f in lineage_facts(
                res,
                target_columns=[c.ref.column for c in cols if c.ref.column is not None],
                provenance=Provenance.DECLARED,
                source=source,
                observed_at=None,
                evidence=False,
            ):
                if f.kind.name == "COLUMN_LINEAGE":
                    self._keep(uid, f)
        self.facts.extend(f for _, f in self._lineage.values())

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


def _import(
    manifest_path: str | os.PathLike[str],
    store: Store,
    bag: DiagnosticBag,
    pool: ParserPool | None,
) -> tuple[list[Fact], bool, dict[str, str]]:
    """``(facts, ok, shas)``; ``ok`` is False when the manifest could not be read or interpreted,
    or its lineage step failed (the 0.1.0 facts are still returned then); ``shas`` is the
    sha256 of each lineage target's ``compiled_code``."""
    path = Path(manifest_path)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        bag.add(Diagnostic("SG209", Severity.ERROR, f"cannot read dbt manifest: {e}", str(path)))
        return [], False, {}
    if not isinstance(manifest, dict):
        bag.add(Diagnostic("SG209", Severity.ERROR, "dbt manifest is not a JSON object", str(path)))
        return [], False, {}
    try:
        imp = _Importer(manifest, store, bag)
        imp.descriptions()
        imp.tests()
    except Exception as e:  # a hostile manifest must never sink the caller
        bag.add(
            Diagnostic("SG209", Severity.ERROR, f"cannot interpret dbt manifest: {e}", str(path))
        )
        return [], False, {}
    try:
        imp.lineage(pool)
    except Exception as e:  # compiled_code is raw SQL: only the exception type is kept
        bag.add(
            Diagnostic(
                "SG209",
                Severity.ERROR,
                f"cannot interpret dbt manifest lineage: {type(e).__name__}",
                str(path),
            )
        )
        return imp.facts, False, {}
    return imp.facts, True, imp.shas


def import_dbt(
    manifest_path: str | os.PathLike[str],
    store: Store,
    *,
    diagnostics: DiagnosticBag | None = None,
    parser_pool: ParserPool | None = None,
) -> list[Fact]:
    """Facts from a dbt manifest; only relations and columns present in ``store`` are mapped.

    Descriptions become DECLARED ``DESCRIPTION``; ``relationships`` -> ``JOIN``,
    ``accepted_values`` -> ``VALUES``; ``unique`` -> ``PRIMARY_KEY`` only as a table's single
    ``unique`` test on a column that also has ``not_null``; ``not_null`` itself is not imported.
    ``depends_on`` -> ``LINEAGE`` (ephemeral models are looked through); ``compiled_code`` ->
    ``COLUMN_LINEAGE`` when the ``lineage`` extra is installed (``SG212`` once otherwise,
    ``SG211`` for a node that does not parse, ``SG214`` for a manifest without compiled code).
    All are DECLARED and UNVERIFIED. An unreadable or malformed manifest is reported as
    ``SG209`` on ``diagnostics`` (if given) and yields no facts; it never raises. A
    ``parser_pool`` that is passed in is not closed.
    """
    bag = diagnostics if diagnostics is not None else DiagnosticBag()
    return _import(manifest_path, store, bag, parser_pool)[0]


def retract_dbt_lineage(
    store: Store,
    loaded: Sequence[Fact],
    *,
    keep_columns: bool = False,
    shas: Mapping[str, str] | None = None,
) -> int:
    """Remove the dbt lineage facts of earlier imports that ``loaded`` no longer contains.

    Candidates are only ``LINEAGE``/``COLUMN_LINEAGE`` facts whose source is a dbt manifest
    node: descriptions, tests, view lineage and human lineage are never touched. The first call
    (no state yet) takes every stored such fact as a candidate. ``keep_columns`` (an import that
    could not compute column lineage) keeps an earlier ``COLUMN_LINEAGE`` fact only while its
    node is still a lineage target in ``shas`` (node id -> sha256 of its ``compiled_code``) with
    the sha recorded when the fact was made; anything else is retracted, never shown as current.
    Returns how many went.
    """
    prefix = f"{SOURCE}:"
    state = store.crawl_state_get(DBT_LINEAGE_STATE)
    raw = None if state is None else state.get("facts")
    if isinstance(raw, list):
        previous = {str(i) for i in raw}
    else:
        previous = {
            f.id
            for f in store.iter_facts()
            if f.kind.name in LINEAGE_KINDS and (f.source or "").startswith(prefix)
        }
    current = {f.id for f in loaded if f.kind.name in LINEAGE_KINDS}
    now = dict(shas or {})
    old = state.get("shas") if state is not None else None
    before = {str(k): str(v) for k, v in old.items()} if isinstance(old, dict) else {}
    kept: set[str] = set()
    if keep_columns:
        for f in store.facts_for(None, ("COLUMN_LINEAGE",)):
            uid = (f.source or "")[len(prefix) :]
            if f.id in previous - current and uid in now and before.get(uid) == now[uid]:
                kept.add(f.id)
    stale = sorted(previous - current - kept)
    removed = store.delete_facts(stale) if stale else 0
    store.crawl_state_set(
        DBT_LINEAGE_STATE, {"facts": sorted(current | kept), "shas": dict(sorted(now.items()))}
    )
    return removed


def apply_dbt(
    manifest_path: str | os.PathLike[str],
    store: Store,
    *,
    diagnostics: DiagnosticBag | None = None,
    parser_pool: ParserPool | None = None,
) -> list[Fact]:
    """:func:`import_dbt`, then store its facts and retract the dbt lineage it no longer
    produces, in one transaction. An unreadable manifest (``SG209``) changes nothing."""
    bag = diagnostics if diagnostics is not None else DiagnosticBag()
    facts, ok, shas = _import(manifest_path, store, bag, parser_pool)
    if not ok:
        return []
    with store.transaction():
        if facts:
            store.put_facts(facts)
        retract_dbt_lineage(store, facts, keep_columns=not lineage_available(), shas=shas)
    return facts
