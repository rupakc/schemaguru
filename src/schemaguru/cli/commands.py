"""The subcommands. Each is ``(args, io) -> exit code`` and only calls the ``Guru`` façade
(or, where the façade has no method, the one library function that does the work)."""

from __future__ import annotations

import argparse
import dataclasses
import importlib
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from schemaguru._util.redact import redact_url, scrub_text
from schemaguru.config import Config
from schemaguru.diagnostics import Diagnostic, DiagnosticBag, Severity
from schemaguru.errors import SchemaGuruError
from schemaguru.guru import Guru
from schemaguru.measure import flbridge
from schemaguru.model.facts import Fact, Provenance, Status
from schemaguru.overlays.dbt import import_dbt as _import_dbt
from schemaguru.overlays.writer import export_drafts as _export_drafts
from schemaguru.store.open import is_postgres_dsn

DEFAULT_STORE = os.path.join(".schemaguru", "default.db")
_DRAFT_ONLY_KEYS = ("prompt_hash", "candidate")
_REJECTED_KEY = "review.rejected"


scrub = scrub_text  # the shared implementation (_util.redact); kept for callers
_SG_CODE = re.compile(r"\bSG\d{3}\b")

# top-level module of a driver or SDK -> the extra that installs it
_MODULE_EXTRAS = {
    "google": "bigquery",
    "snowflake": "snowflake",
    "redshift_connector": "redshift",
    "databricks": "databricks",
    "psycopg": "postgres",
    "psycopg2": "postgres",
    "duckdb": "duckdb",
    "clickhouse_connect": "clickhouse",
    "pyodbc": "mssql",
    "oracledb": "oracle",
    "cx_Oracle": "oracle",
    "trino": "trino",
    "pymysql": "mysql",
    "MySQLdb": "mysql",
    "anthropic": "anthropic",
    "openai": "openai",
    "numpy": "embeddings",
    "tiktoken": "tiktoken",
    "mcp": "mcp",
    "starlette": "server",
    "uvicorn": "server",
    "langchain_core": "langchain",
    "llama_index": "llamaindex",
    "agents": "openai-agents",
}


def missing_module_message(e: ImportError) -> str:
    """What to tell a user whose driver or extra is missing: the module, and the extra that
    installs it when SchemaGuru knows one (a message that already says how is kept)."""
    text = scrub(str(e))
    if "pip install" in text:
        return text
    name = getattr(e, "name", None) or ""
    root = name.split(".", 1)[0]
    extra = _MODULE_EXTRAS.get(root)
    if root == "google" and not name.startswith("google.cloud.bigquery") and name != "google":
        extra = None
    missing = name or "a required module"
    if extra is None:
        return f"{missing} is not installed ({text}); install the driver for this warehouse"
    return f"{missing} is not installed; install it with: pip install 'schemaguru[{extra}]'"


class UsageError(Exception):
    """Invalid input: reported on stderr, exit code 2."""


class IO:
    """Where output goes: results on stdout in ``fmt``; diagnostics and prompts on stderr."""

    def __init__(self, fmt: str = "text", known: Sequence[str | None] = ()) -> None:
        self.fmt = fmt
        self.known = list(known)

    def scrub(self, text: str) -> str:
        return scrub(text, self.known)

    def raw(self, text: str) -> None:
        sys.stdout.write(text if text.endswith("\n") else text + "\n")

    def emit(
        self, data: dict[str, Any], lines: Sequence[str], *, title: str, markdown: str | None = None
    ) -> None:
        """``json``: ``data``; ``markdown``: ``markdown`` or a bulleted ``title`` section."""
        if self.fmt == "json":
            self.raw(json.dumps(data, indent=2, sort_keys=True, default=str, ensure_ascii=False))
        elif self.fmt == "markdown":
            if markdown is not None:
                self.raw(markdown)
            else:
                self.raw("\n".join([f"## {title}", "", *[f"- {line}" for line in lines]]))
        else:
            self.raw("\n".join(lines))

    def error(self, message: str, code: str | None = None) -> None:
        """One error on stderr: ``error: <message>``; with ``--format json`` a JSON object
        ``{"error": {"code": ..., "message": ...}}`` (the code is the message's ``SGnnn`` when
        it has one, else ``code``, else ``"error"``)."""
        text = self.scrub(message)
        if self.fmt == "json":
            found = _SG_CODE.search(text)
            shown = found.group(0) if found else code or "error"
            body = {"error": {"code": shown, "message": text}}
            sys.stderr.write(json.dumps(body, sort_keys=True, ensure_ascii=False) + "\n")
        else:
            sys.stderr.write(f"error: {text}\n")

    def warn(self, message: str) -> None:
        sys.stderr.write(f"warning: {self.scrub(message)}\n")

    def diagnostics(self, items: Sequence[Diagnostic]) -> None:
        for d in items:
            where = f" [{d.location}]" if d.location else ""
            hint = f" (hint: {d.hint})" if d.hint else ""
            line = f"{d.severity.value.lower()} {d.code}{where}: {d.message}{hint}"
            sys.stderr.write(self.scrub(line) + "\n")

    def prompt(self, text: str) -> str | None:
        """One line from stdin (prompt on stderr so ``--format json`` stdout stays clean)."""
        sys.stderr.write(text)
        sys.stderr.flush()
        line = sys.stdin.readline()
        return None if line == "" else line.rstrip("\n")


# ----- shared setup ---------------------------------------------------------------------------


def _config(args: argparse.Namespace) -> Config:
    if args.config:
        try:
            return Config.from_yaml(args.config)
        except FileNotFoundError:
            raise UsageError(f"config file not found: {args.config}") from None
    env = {k: v for k, v in os.environ.items() if k != "SCHEMAGURU_URL"}
    return Config.from_env(environ=env)


def _url(args: argparse.Namespace) -> str | None:
    return args.url or os.environ.get("SCHEMAGURU_URL") or None


@contextmanager
def _guru(
    args: argparse.Namespace, name: str, *, need_url: bool = False, crawl: bool | None = False
) -> Iterator[Guru]:
    """A Guru on ``--url`` (and the store), or on an existing store alone when no URL is given."""
    cfg = _config(args)
    url = _url(args)
    store = args.store or cfg.store_path
    shared: Any = None  # a Postgres store (``--store postgres://...``) is opened here
    if is_postgres_dsn(store):
        shared = _optional("schemaguru.store.postgres", "postgres-store", "the Postgres store")
        shared = shared.PostgresStore(store)
    try:
        if url:
            guru = Guru.connect(url, store=shared or store, config=cfg, crawl=crawl)
        else:
            if need_url:
                raise UsageError(f"{name} needs a warehouse: pass --url or set SCHEMAGURU_URL")
            path = store or DEFAULT_STORE
            if shared is None and not os.path.exists(path):
                raise UsageError(
                    f"no store at {path}: run `schemaguru crawl --url ...` first, or pass --store"
                )
            guru = Guru(store=shared or path, config=cfg)
        try:
            yield guru
        finally:
            guru.close()
    finally:
        if shared is not None:
            shared.close()


def _subject(f: Fact) -> str:
    return str(f.subject.canonical())


def _drafts(guru: Guru) -> list[Fact]:
    """Drafted, non-failed facts not yet accepted or rejected, in a stable order."""
    facts = list(guru.store.iter_facts())
    done = {
        (f.source or "")[len("accepted:") :]
        for f in facts
        if f.provenance is Provenance.HUMAN and (f.source or "").startswith("accepted:")
    }
    rejected = _rejected(guru)
    out = [
        f
        for f in facts
        if f.provenance is Provenance.DRAFTED
        and f.status is not Status.ERROR
        and f.id not in done
        and f.id not in rejected
    ]
    return sorted(out, key=lambda f: (_subject(f), f.kind.name, f.id))


def _rejected(guru: Guru) -> set[str]:
    ids = (guru.store.crawl_state_get(_REJECTED_KEY) or {}).get("ids")
    return {str(i) for i in ids} if isinstance(ids, list) else set()


def _shown_value(f: Fact) -> dict[str, Any]:
    return {k: v for k, v in f.value.items() if k not in _DRAFT_ONLY_KEYS}


# ----- init -----------------------------------------------------------------------------------

_OVERLAY_SKELETON = """\
# schemaguru.yaml - your knowledge about the warehouse (spec 14). Humans own this file;
# SchemaGuru never rewrites it. Anything you declare is checked against measurements and
# a contradiction is reported, never silently "fixed". Uncomment and adapt the examples.
version: 1
warehouse: {warehouse}

# tables:
#   shop.orders:
#     description: One row per order placed on the storefront.
#     use_cases: [revenue by month, average basket size]
#     entity_type: order
#     soft_delete: "deleted IS NULL OR NOT deleted"
#     pitfalls:
#       - rule: Refunds are a second row with negative amount.
#         fix: Filter amount > 0 for gross revenue.
#     columns:
#       status:
#         description: Payment state of the order.
#         values: [paid, refunded, cancelled]   # an expectation: checked, never auto-edited
#       created_at:
#         type_note: ISO-8601 timestamp stored as STRING
#     joins:
#       - to: shop.customers.customer_id
#         from: customer_id
#         cardinality: N:1
#
# chains:
#   - name: order to payment provider
#     because: payments are matched via the order reference, not the customer
#     steps: [shop.orders.order_id -> shop.payments.order_id]
#
# waypoints:
#   - pair: [shop.events, shop.orders]
#     require: "edge(shop.events.session_id->shop.sessions.session_id)"
#     because: events link to orders only through sessions
#
# glossary:
#   - term: active customer
#     definition: ordered in the last 90 days
#     maps_to: [shop.orders, shop.customers]
#
# never_query:
#   - table: shop.orders_backup_2019
#     reason: frozen copy, not maintained
"""

_CONFIG_SKELETON = """\
# schemaguru.config.yaml - every key is optional; the values below are the defaults.
# Environment variables set the same keys: SCHEMAGURU_SCOPE__SEED_K=8,
# SCHEMAGURU_CRAWL__INCLUDE="shop.*,sales.*".

# budget:                          # what measurement may spend (null = unlimited)
#   max_bytes_per_probe: 1073741824
#   max_bytes_total: 10737418240
#   max_probes: null
#   max_seconds: null
#   draft_budget_tokens: null
# sampling:
#   llm_values: masked             # masked | raw | none: what the LLM may see of your data
#   sample_values_per_column: 5
#   measurement: auto              # auto | off | force
# crawl:
#   include: []                    # globs, e.g. ["shop.*"]
#   exclude: ["information_schema.*", "pg_catalog.*", "sys.*", "*.*_staging*", "*.*tmp*"]
# scope:
#   seed_k: 8
# graph:
#   hop_limit: 6
#   k_paths: 3
# enum_max_distinct: 20
# fact_ttl_days: 14
# measure_on_demand: queue         # queue | background | block
# store_path: .schemaguru/default.db
# llm_model: claude-sonnet-5-5
"""


def init(args: argparse.Namespace, io: IO) -> int:
    root = Path(args.dir)
    warehouse = args.warehouse or "my_warehouse"
    files = {
        root / "schemaguru.yaml": _OVERLAY_SKELETON.format(warehouse=json.dumps(warehouse)),
        root / "schemaguru.config.yaml": _CONFIG_SKELETON,
    }
    existing = [str(p) for p in files if p.exists()]
    if existing and not args.force:
        raise UsageError(f"refusing to overwrite {', '.join(existing)} (use --force)")
    root.mkdir(parents=True, exist_ok=True)
    for path, text in files.items():
        path.write_text(text, encoding="utf-8")
    written = [str(p) for p in files]
    url = _url(args)
    lines = [f"wrote {p}" for p in written]
    lines.append(
        "next: schemaguru crawl --url "
        + (redact_url(url) if url else "<warehouse-url>")
        + ' && schemaguru context "<a question>"'
    )
    io.emit({"written": written}, lines, title="init")
    return 0


# ----- crawl, measure, draft ----------------------------------------------------------------


def crawl(args: argparse.Namespace, io: IO) -> int:
    with _guru(args, "crawl", need_url=True, crawl=False) as guru:
        r = guru.crawl(include=args.include, exclude=args.exclude)
    data = {
        "tables_seen": r.tables_seen,
        "tables_written": r.tables_written,
        "withdrawn": r.withdrawn,
        "queries": r.queries,
        "bytes_billed": r.bytes_billed,
        "duration_s": round(r.duration_s, 3),
        "errors": [d.to_dict() for d in r.errors],
    }
    lines = [
        f"tables seen {r.tables_seen}, written {r.tables_written}, withdrawn {r.withdrawn}",
        f"{r.queries} catalog queries in {r.duration_s:.1f}s",
    ]
    io.emit(data, lines, title="Crawl")
    io.diagnostics(r.errors)
    return 3 if r.errors else 0


def measure(args: argparse.Namespace, io: IO) -> int:
    with _guru(args, "measure", need_url=True, crawl=None) as guru:
        budget = None
        over = {
            "max_bytes_total": args.max_bytes,
            "max_probes": args.max_probes,
            "max_seconds": args.max_seconds,
        }
        over = {k: v for k, v in over.items() if v is not None}
        if over:
            budget = dataclasses.replace(guru.config.budget, **over)
        explicit = bool(args.targets) or args.all
        if args.all and args.targets:
            raise UsageError("give table names or --all, not both")
        r = guru.measure(args.targets or None, all=args.all, budget=budget)
    data = {
        "measured_tables": [t.canonical() for t in r.measured_tables],
        "facts_written": r.facts_written,
        "over_budget": [t.canonical() for t in r.over_budget],
        "sampled_tables": [t.canonical() for t in r.sampled_tables],
        "bytes": r.bytes,
        "errors": [d.to_dict() for d in r.errors],
    }
    lines = [
        f"measured {len(r.measured_tables)} table(s), {r.facts_written} fact(s), {r.bytes} bytes"
    ]
    if r.over_budget:
        lines.append(
            "over budget, not measured: " + ", ".join(t.canonical() for t in r.over_budget)
        )
    io.emit(data, lines, title="Measure")
    io.diagnostics(r.errors)
    if r.errors or (explicit and not r.measured_tables):
        if not r.errors:
            io.warn("nothing was measured")
        return 3
    return 0


def _make_provider(name: str, model: str) -> Any:
    """The LLM provider for ``draft`` (a seam tests replace)."""
    try:
        importlib.import_module(name)  # fail now, with the extra to install, not mid-run
    except ImportError:
        raise UsageError(
            f"drafting with {name} needs its SDK: pip install 'schemaguru[{name}]'"
        ) from None
    try:
        if name == "anthropic":
            from schemaguru.draft.providers.anthropic import AnthropicProvider

            return AnthropicProvider(model=model)
        from schemaguru.draft.providers.openai_compat import OpenAICompatibleProvider

        return OpenAICompatibleProvider(
            os.environ.get("OPENAI_BASE_URL"), os.environ.get("OPENAI_API_KEY"), model
        )
    except ImportError as e:
        raise UsageError(str(e)) from None


def draft(args: argparse.Namespace, io: IO) -> int:
    with _guru(args, "draft", need_url=False, crawl=None) as guru:
        provider = _make_provider(args.provider, args.model or guru.config.llm_model)
        r = guru.draft(
            args.targets,
            provider=provider,
            budget=args.budget_tokens,
            verify=not args.no_verify,
        )
    data = {
        "drafted": [f.to_dict() for f in r.drafted],
        "errors": [f.to_dict() for f in r.errors],
        "skipped": [t.canonical() for t in r.skipped],
        "calls": r.calls,
        "tokens": r.tokens,
        "diagnostics": [d.to_dict() for d in r.diagnostics],
    }
    lines = [
        f"drafted {len(r.drafted)} fact(s) from {r.calls} call(s), {r.tokens} token(s); "
        f"{len(r.errors)} failed, {len(r.skipped)} skipped",
        *[f"  {f.id} {f.kind.name} {_subject(f)} [{f.status.value}]" for f in r.drafted],
        "review them with: schemaguru review",
    ]
    io.emit(data, lines, title="Draft")
    io.diagnostics(r.diagnostics)
    return 3 if not r.drafted and (r.errors or r.diagnostics) else 0


# ----- review, accept, export ---------------------------------------------------------------


def review(args: argparse.Namespace, io: IO) -> int:
    with _guru(args, "review") as guru:
        drafts = _drafts(guru)
        if args.table:
            ref = guru.table_ref(args.table).canonical()
            drafts = [f for f in drafts if ref in _subject(f)]
        accepted: list[str] = []
        rejected: list[str] = []
        skipped = 0
        quit_ = False
        for n, f in enumerate(drafts, 1):
            if quit_:
                skipped += 1
                continue
            sys.stderr.write(
                f"\n[{n}/{len(drafts)}] {f.kind.name} {_subject(f)} "
                f"({f.status.value}, {f.source})\n"
                f"  {json.dumps(_shown_value(f), sort_keys=True, ensure_ascii=False)}\n"
            )
            while True:
                ans = io.prompt("accept? [y]es [n]o [e]dit [s]kip [q]uit: ")
                key = (ans or "q").strip().lower()[:1]
                # each decision is applied as it is made: an interrupt loses none of them
                if key == "y":
                    guru.accept([f.id], write_overlay=args.write_overlay)
                    accepted.append(f.id)
                elif key == "n":
                    guru.store.crawl_state_set(
                        _REJECTED_KEY, {"ids": sorted(_rejected(guru) | {f.id})}
                    )
                    rejected.append(f.id)
                elif key == "s":
                    skipped += 1
                elif key == "q":
                    quit_ = True
                    skipped += 1
                elif key == "e":
                    new = _edit(f, io)
                    if new is None:
                        continue
                    guru.store.put_facts([new])
                    guru.accept([new.id], write_overlay=args.write_overlay)
                    accepted.append(new.id)
                else:
                    sys.stderr.write("please answer y, n, e, s or q\n")
                    continue
                break
    data = {
        "accepted": accepted,
        "rejected": rejected,
        "skipped": skipped,
        "reviewed": len(drafts),
    }
    lines = [
        f"{len(drafts)} draft(s): accepted {len(accepted)}, rejected {len(rejected)}, "
        f"skipped {skipped}"
    ]
    io.emit(data, lines, title="Review")
    return 0


def _edit(f: Fact, io: IO) -> Fact | None:
    """Ask for a replacement value (a JSON object); ``None`` re-asks the question."""
    text = io.prompt("new value as JSON (empty = keep): ")
    if text is None:
        return None
    value = _shown_value(f)
    if text.strip():
        try:
            parsed = json.loads(text)
        except ValueError as e:
            sys.stderr.write(f"not valid JSON: {e}\n")
            return None
        if not isinstance(parsed, dict):
            sys.stderr.write("the value must be a JSON object\n")
            return None
        value = parsed
    keep = {k: v for k, v in f.value.items() if k in _DRAFT_ONLY_KEYS}
    changed = value != _shown_value(f)
    return Fact.make(
        f.kind,
        f.subject,
        {**value, **keep},
        Provenance.DRAFTED,
        Status.UNVERIFIED if changed else f.status,
        method=f.method if not changed else None,
        evidence=f.evidence if not changed else None,
        source=f.source,
        observed_at=f.observed_at if not changed else None,
    )


def accept(args: argparse.Namespace, io: IO) -> int:
    with _guru(args, "accept") as guru:
        try:
            facts = guru.accept(args.ids, write_overlay=args.write_overlay)
        except ValueError as e:
            raise UsageError(str(e)) from None
    data = {"accepted": [f.to_dict() for f in facts]}
    lines = [f"accepted {len(facts)} fact(s)"]
    lines += [f"  {f.id} {f.kind.name} {_subject(f)} <- {f.source}" for f in facts]
    io.emit(data, lines, title="Accept")
    return 0


def export_drafts(args: argparse.Namespace, io: IO) -> int:
    with _guru(args, "export-drafts") as guru:
        drafts = _drafts(guru)
        try:
            path = _export_drafts(drafts, args.path)
        except SchemaGuruError as e:
            raise UsageError(str(e)) from None
    io.emit(
        {"path": str(path), "drafts": len(drafts)},
        [f"wrote {len(drafts)} draft(s) to {path}"],
        title="Export drafts",
    )
    return 0


def import_dbt(args: argparse.Namespace, io: IO) -> int:
    bag = DiagnosticBag()
    with _guru(args, "import-dbt") as guru:
        facts = _import_dbt(args.manifest, guru.store, diagnostics=bag)
        if facts:
            guru.store.put_facts(facts)
    kinds = Counter(f.kind.name for f in facts)
    lines = [f"imported {len(facts)} fact(s)"] + [f"  {k}: {n}" for k, n in sorted(kinds.items())]
    io.emit({"imported": len(facts), "kinds": dict(kinds)}, lines, title="dbt import")
    io.diagnostics(list(bag))
    return 2 if bag.errors() else 0


# ----- reading: context, search, path --------------------------------------------------------


def context(args: argparse.Namespace, io: IO) -> int:
    with _guru(args, "context", crawl=None) as guru:
        try:
            view = guru.context(
                args.question,
                budget_tokens=args.budget_tokens,
                profile=args.profile,
                format=io.fmt,
            )
        except ValueError as e:
            raise UsageError(str(e)) from None
        io.raw(str(view))
        io.diagnostics(list(view.diagnostics))
    return 0


def _tool(guru: Guru, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = guru.handle_tool_call(name, arguments)
    if "error" in result:
        # the tool message hides quoted values (it is written for an LLM); the person at the
        # terminal typed the arguments, so they are shown back to them
        given = ", ".join(f"{k}={v}" for k, v in arguments.items())
        raise UsageError(f"{result['text']} [{given}]")
    return result


def search(args: argparse.Namespace, io: IO) -> int:
    with _guru(args, "search", crawl=None) as guru:
        res = _tool(guru, "search_tables", {"query": args.query, "k": args.k})
    io.emit(res, [str(res["text"])], title=f"Tables for {args.query!r}")
    return 0


def path(args: argparse.Namespace, io: IO) -> int:
    with _guru(args, "path", crawl=None) as guru:
        res = _tool(guru, "find_join_path", {"from": args.a, "to": args.b})
    io.emit(res, [str(res["text"])], title=f"{args.a} to {args.b}")
    return 1 if res.get("refusal") else 0


# ----- refresh, report, lint -----------------------------------------------------------------


def refresh(args: argparse.Namespace, io: IO) -> int:
    with _guru(args, "refresh", need_url=True, crawl=False) as guru:
        r = guru.refresh(label=args.label)
    if io.fmt == "json":
        io.raw(r.to_json())
    elif io.fmt == "markdown":
        io.raw(r.to_markdown())
    else:
        n = r.counts()
        lines = [
            f"exit code {r.exit_code}; snapshot {r.snapshot_id}",
            f"tables seen {r.crawl.tables_seen}, measured {len(r.measure.measured_tables)}",
            "changes: " + ", ".join(f"{k.lower()} {v}" for k, v in n.items()),
        ]
        io.raw("\n".join(lines))
    io.diagnostics([*r.crawl.errors, *r.measure.errors])
    return r.exit_code


def report(args: argparse.Namespace, io: IO) -> int:
    with _guru(args, "report") as guru:
        store = guru.store
        live = list(store.iter_tables())
        all_tables = list(store.iter_tables(include_withdrawn=True))
        facts = list(store.iter_facts())
        by_prov = Counter(f"{f.provenance.value}/{f.status.value}" for f in facts)
        by_kind = Counter(f.kind.name for f in facts)
        pending = len(_drafts(guru))
        data = {
            "tables": len(live),
            "withdrawn_tables": len(all_tables) - len(live),
            "facts": len(facts),
            "facts_by_provenance_status": dict(sorted(by_prov.items())),
            "facts_by_kind": dict(sorted(by_kind.items())),
            "drafts_pending_review": pending,
            "latest_snapshot": store.latest_snapshot(),
            "spend": {
                "measure_bytes": store.ledger_total("measure", "bytes", None),
                "measure_queries": store.ledger_total("measure", "queries", None),
                "draft_tokens": store.ledger_total("draft", "tokens", None),
                "crawl_queries": store.ledger_total("crawl", "queries", None),
            },
            "feedback": guru.feedback_summary(),
        }
    lines = [
        f"{data['tables']} live table(s), {data['withdrawn_tables']} withdrawn",
        f"{len(facts)} fact(s); {pending} draft(s) awaiting review",
        f"latest snapshot: {data['latest_snapshot']}",
        "spend: " + ", ".join(f"{k} {v}" for k, v in data["spend"].items()),  # type: ignore[attr-defined]
        *[f"  {k}: {v}" for k, v in sorted(by_prov.items())],
        f"{len(data['feedback'])} failing-query pattern(s) reported",  # type: ignore[arg-type]
    ]
    io.emit(data, lines, title="Report")
    return 0


def _default_overlays() -> list[str]:
    return [p for p in ("schemaguru.yaml", "schemaguru") if os.path.exists(p)]


def lint(args: argparse.Namespace, io: IO) -> int:
    paths = list(args.paths) or _default_overlays()
    if not paths:
        raise UsageError("no overlay files given and no schemaguru.yaml in the current directory")
    missing = [p for p in paths if not os.path.exists(p)]
    if missing:
        raise UsageError(f"overlay not found: {', '.join(missing)}")
    cfg = _config(args)
    store = args.store or cfg.store_path or DEFAULT_STORE
    if not os.path.exists(store) and not _url(args):
        raise UsageError(f"no store at {store}: crawl first so table references can be checked")
    # lint on a copy: loading overlays writes their facts to the store it is given
    with tempfile.TemporaryDirectory(prefix="schemaguru-lint-") as tmp:
        if os.path.exists(store):
            copy = os.path.join(tmp, "lint.db")
            shutil.copy2(store, copy)
            largs = argparse.Namespace(**{**vars(args), "store": copy, "url": None})
        else:
            largs = args
        with _guru(largs, "lint", crawl=None) as guru:
            diags = guru.load_overlays(*paths)
    findings = [d for d in diags if d.severity is not Severity.INFO]
    data = {"paths": paths, "diagnostics": [d.to_dict() for d in diags]}
    lines = [f"{len(paths)} overlay path(s): {len(findings)} finding(s)"]
    lines += [f"  {d.severity.value} {d.code} {d.location or ''} {d.message}" for d in diags]
    io.emit(data, lines, title="Lint")
    return 1 if findings else 0


# ----- serve, mcp, worker --------------------------------------------------------------------


def _optional(module: str, extra: str, what: str) -> Any:
    try:
        return importlib.import_module(module)
    except ImportError as e:
        raise UsageError(
            f"{what} is not available ({e}); install it with: pip install 'schemaguru[{extra}]'"
        ) from None


def serve(args: argparse.Namespace, io: IO) -> int:
    server = _optional("schemaguru.server", "server", "the HTTP server")
    entry = getattr(server, "serve", None)
    if not callable(entry):
        raise UsageError(
            "the HTTP server is not available in this build; "
            "install it with: pip install 'schemaguru[server]'"
        )
    with _guru(args, "serve", crawl=None) as guru:
        entry(guru, host=args.host, port=args.port)
    return 0


def mcp(args: argparse.Namespace, io: IO) -> int:
    module = _optional("schemaguru.agent.mcp", "mcp", "the MCP server")
    _optional("mcp.server.lowlevel", "mcp", "the MCP SDK")  # the module imports it lazily
    entry = getattr(module, "serve", None)
    if not callable(entry):
        raise UsageError(
            "the MCP server is not available in this build; "
            "install it with: pip install 'schemaguru[mcp]'"
        )
    with _guru(args, "mcp", crawl=None) as guru:
        entry(guru)
    return 0


def worker(args: argparse.Namespace, io: IO) -> int:
    code = 0
    with _guru(args, "worker", need_url=True, crawl=None) as guru:
        while True:
            r = guru.measure_queue()
            if r.errors:
                io.diagnostics(r.errors)
                code = 3
            sys.stderr.write(
                f"measured {len(r.measured_tables)} table(s), {r.facts_written} fact(s)\n"
            )
            if args.once:
                break
            time.sleep(max(args.interval, 0.1))
    return code


# ----- doctor --------------------------------------------------------------------------------

_EXTRAS: dict[str, str] = {
    "duckdb": "duckdb",
    "bigquery": "google.cloud.bigquery",
    "snowflake": "snowflake.connector",
    "redshift": "redshift_connector",
    "databricks": "databricks.sql",
    "postgres": "psycopg",
    "clickhouse": "clickhouse_connect",
    "mssql": "pyodbc",
    "oracle": "oracledb",
    "trino": "trino",
    "mysql": "pymysql",
    "anthropic": "anthropic",
    "openai": "openai",
    "embeddings": "numpy",
    "tiktoken": "tiktoken",
    "mcp": "mcp",
    "server": "starlette",
}
_DRIVERS = {"duckdb", "bigquery", "snowflake", "redshift", "databricks", "postgres"} | {
    "clickhouse",
    "mssql",
    "oracle",
    "trino",
    "mysql",
}
_FACTLANG_WANTED = ("measure", "estimate", "sample", "composite_join", "run_metadata")


def _have(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _check(name: str, status: str, detail: str, hint: str | None = None) -> dict[str, Any]:
    return {"check": name, "status": status, "detail": detail, "hint": hint}


def _doctor_checks(args: argparse.Namespace) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    cfg: Config | None = None
    try:
        cfg = _config(args)
    except (UsageError, SchemaGuruError) as e:
        out.append(
            _check("config", "FAIL", str(e), "fix the config file or SCHEMAGURU_* variables")
        )

    url = _url(args)
    if not url:
        out.append(
            _check("connectivity", "WARN", "no --url given", "pass --url or set SCHEMAGURU_URL")
        )
    else:
        probe = tempfile.mkdtemp(prefix="schemaguru-doctor-")
        try:
            guru = Guru.connect(url, store=os.path.join(probe, "p.db"), crawl=False)
            try:
                reader = guru.reader
                if reader is None:
                    raise SchemaGuruError("no catalog reader for this URL")
                schemas = reader.list_schemas(None, page_token=None).items
                out.append(
                    _check(
                        "connectivity",
                        "PASS",
                        f"connected to {redact_url(url)} ({guru.dialect}); "
                        f"{len(schemas)} schema(s) readable",
                    )
                )
            finally:
                guru.close()
        except Exception as e:
            out.append(
                _check(
                    "connectivity",
                    "FAIL",
                    f"{type(e).__name__}: {scrub(str(e), [url])}",
                    "check the URL, credentials and network; install the warehouse extra",
                )
            )
        finally:
            shutil.rmtree(probe, ignore_errors=True)

    try:
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        finally:
            conn.close()
        out.append(_check("fts5", "PASS", "SQLite FTS5 is available"))
    except sqlite3.Error:
        out.append(
            _check(
                "fts5",
                "WARN",
                "SQLite FTS5 is missing; search falls back to slower substring matching",
                "use a Python build whose SQLite has FTS5",
            )
        )

    caps = flbridge.capabilities()
    missing = [c for c in _FACTLANG_WANTED if c not in caps]
    if not caps:
        out.append(
            _check(
                "factlang",
                "WARN",
                "no FactLang capability detected: measurement reports SG304",
                "pip install -U 'factlang>=0.1.3,<0.2'",
            )
        )
    elif missing:
        out.append(
            _check(
                "factlang",
                "WARN",
                f"capabilities: {', '.join(sorted(caps))}; missing: {', '.join(missing)}",
                "upgrade FactLang to enable measurement features",
            )
        )
    else:
        out.append(_check("factlang", "PASS", f"capabilities: {', '.join(sorted(caps))}"))

    installed = sorted(e for e, m in _EXTRAS.items() if _have(m))
    if installed and _DRIVERS & set(installed):
        out.append(_check("extras", "PASS", f"installed: {', '.join(installed)}"))
    else:
        out.append(
            _check(
                "extras",
                "WARN",
                f"installed: {', '.join(installed) or 'none'}; no warehouse driver extra found",
                "pip install 'schemaguru[duckdb]' (or your warehouse's extra)",
            )
        )

    if cfg is not None:
        b = cfg.budget
        unbounded = [
            n
            for n, v in (
                ("max_bytes_per_probe", b.max_bytes_per_probe),
                ("max_bytes_total", b.max_bytes_total),
            )
            if v is None
        ]
        if unbounded:
            out.append(
                _check(
                    "budgets",
                    "WARN",
                    f"unbounded: {', '.join(unbounded)}",
                    "set budget.max_bytes_per_probe and budget.max_bytes_total in the config",
                )
            )
        else:
            out.append(
                _check(
                    "budgets",
                    "PASS",
                    f"per probe {b.max_bytes_per_probe} bytes, total {b.max_bytes_total} bytes",
                )
            )
    return out


def doctor(args: argparse.Namespace, io: IO) -> int:
    checks = _doctor_checks(args)
    lines = []
    for c in checks:
        lines.append(f"{c['status']:4} {c['check']}: {c['detail']}")
        if c["hint"] and c["status"] != "PASS":
            lines.append(f"     hint: {c['hint']}")
    io.emit({"checks": checks}, lines, title="Doctor")
    return 3 if any(c["status"] == "FAIL" for c in checks) else 0
