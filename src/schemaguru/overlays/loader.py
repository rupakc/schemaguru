"""Load human overlay YAML into facts, chains, waypoints and waivers (spec 14).

Loading never raises on data: every problem is a line-numbered ``SG2xx`` diagnostic and the
offending entry is skipped. An empty YAML key (``key:``) is the same as an absent one.
"""

from __future__ import annotations

import difflib
from collections.abc import Iterable, Sequence
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from schemaguru.diagnostics import Diagnostic, DiagnosticBag, Severity
from schemaguru.model.chains import Chain, FilterStep, NoteStep, Step
from schemaguru.model.facts import Fact, Provenance, Status, Waiver
from schemaguru.model.joins import JoinRef
from schemaguru.model.kinds import FactKind, KindSpec
from schemaguru.model.refs import ObjectRef
from schemaguru.overlays import schema as S
from schemaguru.overlays.schema import WaypointSpec
from schemaguru.store.protocol import Store

_MAX_BYTES = 8 * 1024 * 1024


def _is_null(node: yaml.Node) -> bool:
    return isinstance(node, yaml.ScalarNode) and (
        node.tag == "tag:yaml.org,2002:null" or (node.value == "" and node.style is None)
    )


class _FileLoader:
    def __init__(self, path: str, store: Store, bag: DiagnosticBag, today: date) -> None:
        self.path = path
        self.store = store
        self.bag = bag
        self.today = today
        self.facts: list[Fact] = []
        self.chains: list[Chain] = []
        self.waypoints: list[WaypointSpec] = []
        self.waivers: list[Waiver] = []
        self._ctor = yaml.SafeLoader("")

    # ----- diagnostics ----------------------------------------------------------------------

    def line(self, node: yaml.Node) -> int:
        return int(node.start_mark.line) + 1

    def loc(self, node: yaml.Node | None) -> str:
        return self.path if node is None else f"{self.path}:{self.line(node)}"

    def diag(
        self,
        code: str,
        message: str,
        node: yaml.Node | None,
        *,
        hint: str | None = None,
        severity: Severity = Severity.ERROR,
    ) -> None:
        self.bag.add(Diagnostic(code, severity, message, self.loc(node), hint))

    def source(self, node: yaml.Node) -> str:
        return f"overlay:{self.path}:{self.line(node)}"

    # ----- node access ----------------------------------------------------------------------

    def scalar(self, node: yaml.Node) -> Any:
        try:
            return self._ctor.construct_object(node, deep=True)
        except Exception:  # malformed scalars are data errors, never crashes
            return None

    def mapping(
        self, node: yaml.Node, allowed: Sequence[str], where: str
    ) -> dict[str, yaml.Node] | None:
        """Present (non-empty) entries of a mapping; unknown keys are SG202. ``None`` if absent."""
        if _is_null(node):
            return None
        if not isinstance(node, yaml.MappingNode):
            self.diag("SG208", f"{where}: expected a mapping", node)
            return None
        out: dict[str, yaml.Node] = {}
        for knode, vnode in node.value:
            if not isinstance(knode, yaml.ScalarNode):
                self.diag("SG208", f"{where}: keys must be plain strings", knode)
                continue
            key = str(self.scalar(knode))
            if key not in allowed:
                near = difflib.get_close_matches(key, allowed, n=1)
                self.diag(
                    "SG202",
                    f"{where}: unknown key {key!r}",
                    knode,
                    hint=f"did you mean {near[0]!r}?" if near else f"allowed: {', '.join(allowed)}",
                )
                continue
            if _is_null(vnode):
                continue
            out[key] = vnode
        return out

    def items(self, node: yaml.Node, where: str) -> list[yaml.Node]:
        if _is_null(node):
            return []
        if not isinstance(node, yaml.SequenceNode):
            self.diag("SG208", f"{where}: expected a list", node)
            return []
        return [n for n in node.value if not _is_null(n)]

    def text(self, node: yaml.Node | None, where: str) -> str | None:
        if node is None:
            return None
        if not isinstance(node, yaml.ScalarNode):
            self.diag("SG208", f"{where}: expected text", node)
            return None
        v = self.scalar(node)
        if v is None:
            return None
        if node.style is None:  # unquoted: keep the source text (01, 1.10, 0x1F, yes, dates)
            return str(node.value)
        return str(v)

    def texts(self, node: yaml.Node | None, where: str) -> list[str] | None:
        """A scalar or a list of scalars as a list of strings."""
        if node is None:
            return None
        if isinstance(node, yaml.ScalarNode):
            t = self.text(node, where)
            return None if t is None else [t]
        out = [t for n in self.items(node, where) if (t := self.text(n, where)) is not None]
        return out

    # ----- ref resolution -------------------------------------------------------------------

    def _fail(self, text: str, cands: list[ObjectRef], node: yaml.Node, what: str) -> None:
        if cands:
            names = ", ".join(c.canonical() for c in cands)
            self.diag(
                "SG204",
                f"ambiguous {what} {text!r}: matches {names}",
                node,
                hint="write the exact name as the catalog stores it",
            )
        else:
            self.diag("SG203", f"cannot resolve {what} {text!r} in the store", node)

    def table(self, text: str, node: yaml.Node) -> ObjectRef | None:
        ref, cands = self.store.resolve(text)
        if ref is None or ref.column is not None:
            if ref is not None:
                self.diag("SG203", f"{text!r} is a column, expected a table", node)
            else:
                self._fail(text, cands, node, "table")
            return None
        return ref

    def column(self, table: ObjectRef, name: str, node: yaml.Node) -> ObjectRef | None:
        names = [c.ref.column for c in self.store.columns(table) if c.ref.column is not None]

        def make(n: str) -> ObjectRef:
            return ObjectRef(table.catalog, table.schema, table.table, n)

        if name in names:
            return make(name)
        ci = [n for n in names if n.lower() == name.lower()]
        if len(ci) == 1:
            return make(ci[0])
        self._fail(f"{table.canonical()}.{name}", [make(n) for n in ci], node, "column")
        return None

    def column_ref(self, text: str, node: yaml.Node) -> ObjectRef | None:
        try:
            parsed = ObjectRef.parse(text, has_column=True)
        except ValueError as e:
            self.diag("SG203", f"invalid column reference {text!r}: {e}", node)
            return None
        assert parsed.column is not None
        tbl = self.table(parsed.table_ref().canonical(), node)
        return None if tbl is None else self.column(tbl, parsed.column, node)

    def filter_ok(self, text: str, node: yaml.Node, where: str) -> bool:
        problem = S.check_filter_syntax(text)
        if problem is None:
            return True
        self.diag("SG205", f"{where}: invalid filter {text!r}: {problem}", node)
        return False

    # ----- fact construction ----------------------------------------------------------------

    def add(
        self, kind: KindSpec, subject: ObjectRef | JoinRef, value: dict[str, Any], node: yaml.Node
    ) -> None:
        self.facts.append(
            Fact.make(
                kind,
                subject,
                value,
                Provenance.HUMAN,
                Status.UNVERIFIED,
                source=self.source(node),
            )
        )

    # ----- sections -------------------------------------------------------------------------

    def run(self, root: yaml.Node) -> None:
        top = self.mapping(root, S.TOP_KEYS, "overlay")
        if top is None:
            return
        ver = top.get("version")
        if (
            ver is None
            or self.scalar(ver) != S.OVERLAY_VERSION
            or isinstance(self.scalar(ver), bool)
        ):
            self.diag(
                "SG201",
                "missing or unsupported 'version' (this library reads version: 1)",
                ver if ver is not None else root,
                hint="add 'version: 1' at the top of the file",
            )
            return
        if "warehouse" in top:
            self.text(top["warehouse"], "warehouse")
        if "tables" in top:
            self.tables(top["tables"])
        if "chains" in top:
            for n in self.items(top["chains"], "chains"):
                self.chain(n)
        if "waypoints" in top:
            for n in self.items(top["waypoints"], "waypoints"):
                self.waypoint(n)
        if "glossary" in top:
            for n in self.items(top["glossary"], "glossary"):
                self.glossary(n)
        if "never_query" in top:
            for n in self.items(top["never_query"], "never_query"):
                self.never_query(n)
        if "waivers" in top:
            for n in self.items(top["waivers"], "waivers"):
                self.waiver(n)

    def tables(self, node: yaml.Node) -> None:
        if not isinstance(node, yaml.MappingNode):
            self.diag("SG208", "tables: expected a mapping of table -> entry", node)
            return
        for knode, vnode in node.value:
            if not isinstance(knode, yaml.ScalarNode):
                self.diag("SG208", "tables: keys must be plain strings", knode)
                continue
            tref = self.table(str(self.scalar(knode)), knode)
            if tref is None:
                continue
            self.table_entry(tref, vnode)

    def table_entry(self, tref: ObjectRef, node: yaml.Node) -> None:
        where = f"tables.{tref.canonical()}"
        m = self.mapping(node, S.TABLE_KEYS, where)
        if m is None:
            return
        if (n := m.get("description")) is not None and (t := self.text(n, where)) is not None:
            self.add(FactKind.DESCRIPTION, tref, {"text": t}, n)
        if "use_cases" in m:
            qs = self.texts(m["use_cases"], f"{where}.use_cases")
            if qs:
                self.add(FactKind.USE_CASES, tref, {"questions": qs}, m["use_cases"])
        if (n := m.get("entity_type")) is not None and (t := self.text(n, where)) is not None:
            self.add(FactKind.ENTITY_TYPE, tref, {"entity": t}, n)
        if (n := m.get("soft_delete")) is not None:
            t = self.text(n, f"{where}.soft_delete")
            if t is not None and self.filter_ok(t, n, f"{where}.soft_delete"):
                self.add(FactKind.SOFT_DELETE, tref, {"predicate": t, "marker_columns": None}, n)
        for p in self.items(m["pitfalls"], f"{where}.pitfalls") if "pitfalls" in m else []:
            self.pitfall(tref, p)
        if "columns" in m:
            self.columns(tref, m["columns"])
        for j in self.items(m["joins"], f"{where}.joins") if "joins" in m else []:
            self.join(tref, j)

    def pitfall(self, tref: ObjectRef, node: yaml.Node) -> None:
        where = f"tables.{tref.canonical()}.pitfalls"
        if isinstance(node, yaml.ScalarNode):  # shorthand: a bare rule
            rule = self.text(node, where)
            if rule is not None:
                self.add(
                    FactKind.PITFALL, tref, {"rule": rule, "fix": None, "severity": "warn"}, node
                )
            return
        m = self.mapping(node, S.PITFALL_KEYS, where)
        if m is None:
            return
        rule = self.text(m.get("rule"), where)
        if rule is None:
            self.diag("SG208", f"{where}: a pitfall needs a 'rule'", node)
            return
        sev = self.text(m.get("severity"), where) or "warn"
        if sev not in S.SEVERITIES:
            self.diag("SG208", f"{where}: severity must be one of {S.SEVERITIES}", m["severity"])
            sev = "warn"
        fix = self.text(m.get("fix"), where)
        self.add(FactKind.PITFALL, tref, {"rule": rule, "fix": fix, "severity": sev}, node)

    def columns(self, tref: ObjectRef, node: yaml.Node) -> None:
        where = f"tables.{tref.canonical()}.columns"
        if _is_null(node):
            return
        if not isinstance(node, yaml.MappingNode):
            self.diag("SG208", f"{where}: expected a mapping of column -> entry", node)
            return
        for knode, vnode in node.value:
            if not isinstance(knode, yaml.ScalarNode):
                self.diag("SG208", f"{where}: keys must be plain strings", knode)
                continue
            cref = self.column(tref, str(self.scalar(knode)), knode)
            if cref is not None:
                self.column_entry(cref, vnode)

    def column_entry(self, cref: ObjectRef, node: yaml.Node) -> None:
        where = f"columns.{cref.canonical()}"
        m = self.mapping(node, S.COLUMN_KEYS, where)
        if m is None:
            return
        if (n := m.get("description")) is not None and (t := self.text(n, where)) is not None:
            self.add(FactKind.DESCRIPTION, cref, {"text": t}, n)
        if "values" in m:
            vals = self.texts(m["values"], f"{where}.values")
            if vals:
                split = self.text(m.get("values_split"), where)
                self.add(
                    FactKind.VALUES,
                    cref,
                    {"values": vals, "case_sensitive": True, "split": split},
                    m["values"],
                )
        elif "values_split" in m:
            self.diag("SG208", f"{where}: 'values_split' without 'values'", m["values_split"])
        if (n := m.get("type_note")) is not None and (t := self.text(n, where)) is not None:
            self.add(FactKind.TYPE_NOTE, cref, {"stored_as": None, "means": t}, n)

    def join(self, tref: ObjectRef, node: yaml.Node) -> None:
        where = f"tables.{tref.canonical()}.joins"
        m = self.mapping(node, S.JOIN_KEYS, where)
        if m is None:
            return
        to_texts = self.texts(m.get("to"), f"{where}.to")
        from_texts = self.texts(m.get("from"), f"{where}.from")
        if not to_texts or not from_texts:
            self.diag("SG208", f"{where}: a join needs both 'to' and 'from'", node)
            return
        if len(to_texts) != len(from_texts):
            self.diag(
                "SG208", f"{where}: 'to' and 'from' must list the same number of columns", node
            )
            return
        left = [self.column(tref, t, m["from"]) for t in from_texts]
        right = [self.column_ref(t, m["to"]) for t in to_texts]
        if any(c is None for c in left) or any(c is None for c in right):
            return
        lcols = tuple(c for c in left if c is not None)
        rcols = tuple(c for c in right if c is not None)
        if len({c.table_ref() for c in rcols}) != 1:
            self.diag("SG208", f"{where}: all 'to' columns must belong to one table", m["to"])
            return
        card = self.text(m.get("cardinality"), where)
        if card is not None and card not in S.CARDINALITIES:
            self.diag(
                "SG208", f"{where}: cardinality must be one of {S.CARDINALITIES}", m["cardinality"]
            )
            card = None
        cast = self.text(m.get("cast_to"), where)
        self.add(FactKind.JOIN, JoinRef(lcols, rcols, cast), {"cardinality": card}, node)

    def parse_join_step(self, text: str, node: yaml.Node) -> JoinRef | None:
        if "->" not in text:
            self.diag("SG208", f"chain step {text!r}: expected 'a.b.c -> x.y.z'", node)
            return None
        lhs, _, rhs = text.partition("->")
        sides: list[tuple[ObjectRef, ...]] = []
        for side in (lhs, rhs):
            cols = [self.column_ref(p.strip(), node) for p in side.split(",") if p.strip()]
            if not cols or any(c is None for c in cols):
                return None
            sides.append(tuple(c for c in cols if c is not None))
        try:
            return JoinRef(sides[0], sides[1])
        except ValueError as e:
            self.diag("SG208", f"chain step {text!r}: {e}", node)
            return None

    def chain(self, node: yaml.Node) -> None:
        m = self.mapping(node, S.CHAIN_KEYS, "chains")
        if m is None:
            return
        name = self.text(m.get("name"), "chains")
        because = self.text(m.get("because"), "chains")
        if name is None or because is None:
            self.diag("SG208", "chains: every chain needs 'name' and 'because'", node)
            return
        steps: list[Step] = []
        ok = True
        for sn in self.items(m["steps"], "chains.steps") if "steps" in m else []:
            step = self.chain_step(sn)
            if step is None:
                ok = False
            else:
                steps.append(step)
        if not steps and "steps" not in m:
            self.diag("SG208", f"chains.{name}: a chain needs 'steps'", node)
            return
        if ok and steps:
            self.chains.append(Chain(name, tuple(steps), because))

    def chain_step(self, node: yaml.Node) -> Step | None:
        if isinstance(node, yaml.ScalarNode):
            t = self.text(node, "chains.steps")
            return None if t is None else self.parse_join_step(t, node)
        m = self.mapping(node, S.STEP_KEYS, "chains.steps")
        if m is None:
            return None
        if "note" in m:
            t = self.text(m["note"], "chains.steps")
            return None if t is None else NoteStep(t)
        if "filter" in m:
            t = self.text(m["filter"], "chains.steps")
            tn = self.text(m.get("table"), "chains.steps")
            if t is None or tn is None:
                self.diag("SG208", "chains.steps: a filter step needs 'filter' and 'table'", node)
                return None
            tref = self.table(tn, m["table"])
            if tref is None or not self.filter_ok(t, m["filter"], "chains.steps"):
                return None
            return FilterStep(tref, t)
        self.diag("SG208", "chains.steps: a step is 'a -> b', {filter, table} or {note}", node)
        return None

    def waypoint(self, node: yaml.Node) -> None:
        m = self.mapping(node, S.WAYPOINT_KEYS, "waypoints")
        if m is None:
            return
        scope: str
        if "pair" in m:
            scope, tnodes = "pair", m["pair"]
        elif "node" in m:
            scope, tnodes = "node", m["node"]
        else:
            self.diag("SG208", "waypoints: a rule needs 'pair' or 'node'", node)
            return
        names = self.texts(tnodes, "waypoints")
        if (
            not names
            or (scope == "pair" and len(names) != 2)
            or (scope == "node" and len(names) != 1)
        ):
            self.diag(
                "SG208",
                f"waypoints: '{scope}' takes {2 if scope == 'pair' else 1} table(s)",
                tnodes,
            )
            return
        refs = [self.table(t, tnodes) for t in names]
        if any(r is None for r in refs):
            return
        require = self.texts(m.get("require"), "waypoints.require") or []
        forbid = self.texts(m.get("forbid"), "waypoints.forbid") or []
        because = self.text(m.get("because"), "waypoints")
        if scope == "pair" and not require and not forbid:  # `node: X` alone forbids X
            self.diag("SG208", "waypoints: a pair rule needs 'require' or 'forbid'", node)
            return
        if because is None:
            self.diag("SG208", "waypoints: a rule needs 'because'", node)
            return
        self.waypoints.append(
            WaypointSpec(
                "pair" if scope == "pair" else "node",
                tuple(r for r in refs if r is not None),
                tuple(require),
                tuple(forbid),
                because,
                self.source(node),
            )
        )

    def glossary(self, node: yaml.Node) -> None:
        m = self.mapping(node, S.GLOSSARY_KEYS, "glossary")
        if m is None:
            return
        term = self.text(m.get("term"), "glossary")
        definition = self.text(m.get("definition"), "glossary")
        if term is None or definition is None:
            self.diag("SG208", "glossary: every entry needs 'term' and 'definition'", node)
            return
        maps: list[ObjectRef] = []
        for t in self.texts(m.get("maps_to"), "glossary.maps_to") or []:
            ref, cands = self.store.resolve(t)
            if ref is None:
                self._fail(t, cands, m["maps_to"], "table")
                continue
            maps.append(ref)
        self.add(
            FactKind.GLOSSARY,
            S.GLOBAL_SUBJECT,
            {"term": term, "definition": definition, "maps_to": [r.to_dict() for r in maps]},
            node,
        )

    def never_query(self, node: yaml.Node) -> None:
        m = self.mapping(node, S.NEVER_QUERY_KEYS, "never_query")
        if m is None:
            return
        tn = self.text(m.get("table"), "never_query")
        reason = self.text(m.get("reason"), "never_query")
        if tn is None or reason is None:
            self.diag("SG208", "never_query: every entry needs 'table' and 'reason'", node)
            return
        tref = self.table(tn, m["table"])
        if tref is not None:
            self.add(FactKind.NEVER_QUERY, tref, {"reason": reason}, node)

    def waiver(self, node: yaml.Node) -> None:
        m = self.mapping(node, S.WAIVER_KEYS, "waivers")
        if m is None:
            return
        pattern = self.text(m.get("fact"), "waivers")
        reason = self.text(m.get("reason"), "waivers")
        if pattern is None or reason is None:
            self.diag("SG208", "waivers: every waiver needs 'fact' and 'reason'", node)
            return
        exp_node = m.get("expires")
        if exp_node is None:
            self.diag(
                "SG206",
                f"waiver for {pattern!r} has no 'expires'; it is ignored",
                node,
                hint="add 'expires: YYYY-MM-DD'",
            )
            return
        expires = self.parse_date(exp_node)
        if expires is None:
            self.diag("SG206", f"waiver for {pattern!r}: 'expires' is not a date", exp_node)
            return
        if expires < self.today:
            self.diag(
                "SG207",
                f"waiver for {pattern!r} expired on {expires.isoformat()}; it is ignored",
                exp_node,
                severity=Severity.WARNING,
            )
            return
        try:
            self.waivers.append(Waiver(pattern, reason, expires))
        except ValueError as e:
            self.diag(
                "SG208", f"waivers: {e}", m["fact"], hint='e.g. fact: "VALUES shop.orders.status"'
            )

    def parse_date(self, node: yaml.Node) -> date | None:
        v = self.scalar(node) if isinstance(node, yaml.ScalarNode) else None
        if isinstance(v, datetime):
            return v.date()
        if isinstance(v, date):
            return v
        if isinstance(v, str):
            try:
                return date.fromisoformat(v.strip())
            except ValueError:
                return None
        return None


def _expand(paths: Iterable[str | Path]) -> list[Path]:
    out: list[Path] = []
    for p in paths:
        pp = Path(p)
        if pp.is_dir():
            out.extend(sorted([*pp.glob("*.yaml"), *pp.glob("*.yml")]))
        else:
            out.append(pp)
    return out


def load_overlays(
    paths: Sequence[str | Path], store: Store, *, today: date | None = None
) -> tuple[list[Fact], list[Chain], list[WaypointSpec], list[Waiver], DiagnosticBag]:
    """Load overlay files (a directory contributes its ``*.yaml`` / ``*.yml``, non-recursive).

    Never raises on data. ``today`` (default: the current UTC date) decides waiver expiry.
    """
    bag = DiagnosticBag()
    now = today or datetime.now(timezone.utc).date()
    facts: list[Fact] = []
    chains: list[Chain] = []
    waypoints: list[WaypointSpec] = []
    waivers: list[Waiver] = []
    for path in _expand(paths):
        fl = _FileLoader(str(path), store, bag, now)
        try:
            if path.stat().st_size > _MAX_BYTES:
                bag.add(Diagnostic("SG208", Severity.ERROR, "overlay file too large", str(path)))
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            bag.add(Diagnostic("SG208", Severity.ERROR, f"cannot read overlay: {e}", str(path)))
            continue
        try:
            root = yaml.compose(text, Loader=yaml.SafeLoader)
        except yaml.YAMLError as e:
            mark = getattr(e, "problem_mark", None)
            where = f"{path}:{mark.line + 1}" if mark is not None else str(path)
            bag.add(Diagnostic("SG208", Severity.ERROR, f"invalid YAML: {e}", where))
            continue
        except Exception as e:  # never sink the load on hostile input
            bag.add(Diagnostic("SG208", Severity.ERROR, f"invalid YAML: {e}", str(path)))
            continue
        if root is None:
            continue
        try:
            fl.run(root)
        except RecursionError:
            bag.add(Diagnostic("SG208", Severity.ERROR, "overlay nested too deeply", str(path)))
        facts += fl.facts
        chains += fl.chains
        waypoints += fl.waypoints
        waivers += fl.waivers
    return facts, chains, waypoints, waivers, bag


# crawl_state key recording the ids of the facts the last overlay load put in the store
OVERLAY_STATE = "overlay_facts"
_SOURCE = "overlay:"
# file-level failures: a file reporting one keeps its previous facts until it loads cleanly
_FILE_ERRORS = frozenset({"SG201", "SG208"})


def _source_file(source: str | None) -> str | None:
    if not source or not source.startswith(_SOURCE):
        return None
    return source[len(_SOURCE) :].rsplit(":", 1)[0]


def retract_removed(store: Store, loaded: Sequence[Fact], diagnostics: Iterable[Diagnostic]) -> int:
    """Remove the overlay facts of earlier loads that this load no longer produces (17b).

    A load replaces the overlay knowledge: an entry removed from its file (or a file no longer
    passed) loses its HUMAN facts. A file that failed to load (``SG201``/``SG208``) keeps its
    previous facts, so a typo never wipes knowledge. Only facts whose source is an overlay are
    candidates; measured, accepted and dbt facts are never touched. Returns how many went.
    """
    state = store.crawl_state_get(OVERLAY_STATE)
    previous: dict[str, str] = {}
    if state is None:  # first load with retraction: every stored overlay fact is a candidate
        for f in store.iter_facts():
            if (path := _source_file(f.source)) is not None:
                previous[f.id] = path
    else:
        raw = state.get("facts")
        previous = {str(i): str(p) for i, p in raw.items()} if isinstance(raw, dict) else {}
    failed = [
        d.location
        for d in diagnostics
        if d.code in _FILE_ERRORS and d.severity is Severity.ERROR and d.location
    ]
    keep_files = {p for p in previous.values() if any(_same_file(p, loc) for loc in failed)}
    current = {f.id: _source_file(f.source) or "" for f in loaded}
    kept = {i: p for i, p in previous.items() if p in keep_files and i not in current}
    stale = sorted(i for i, p in previous.items() if i not in current and p not in keep_files)
    removed = store.delete_facts(stale) if stale else 0
    store.crawl_state_set(OVERLAY_STATE, {"facts": {**kept, **current}})
    return removed


def _same_file(source_file: str, location: str) -> bool:
    """Does a diagnostic location (``path`` or ``path:line``) name ``source_file``?"""
    return location == source_file or location.startswith(source_file + ":")
