"""Argument parsing and dispatch. Exit codes (spec 19.1): 0 ok, 1 review-class findings,
2 invalid input, 3 errors or nothing measured."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence

from schemaguru.__about__ import __version__
from schemaguru.catalog.executor import _redact
from schemaguru.cli import commands as cmd
from schemaguru.errors import ConfigError, SchemaGuruError

EXIT_OK, EXIT_FINDINGS, EXIT_INVALID, EXIT_ERROR = 0, 1, 2, 3

_GLOBALS = argparse.ArgumentParser(add_help=False)
# the same flags after the command; SUPPRESS keeps a value given before the command
_GLOBALS.add_argument("--url", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
_GLOBALS.add_argument("--store", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
_GLOBALS.add_argument("--config", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
_GLOBALS.add_argument(
    "--format",
    choices=("text", "json", "markdown"),
    default=argparse.SUPPRESS,
    help=argparse.SUPPRESS,
)

Handler = Callable[[argparse.Namespace, cmd.IO], int]


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="schemaguru",
        description="Automated schema intelligence for data warehouses.",
    )
    p.add_argument("--version", action="version", version=f"schemaguru {__version__}")
    p.add_argument("--url", default=None, help="warehouse URL (or $SCHEMAGURU_URL)")
    p.add_argument("--store", default=None, help="store path (default .schemaguru/default.db)")
    p.add_argument("--config", default=None, help="a schemaguru.config.yaml (else environment)")
    p.add_argument(
        "--format",
        choices=("text", "json", "markdown"),
        default="text",
        help="output format (json: parseable on stdout; diagnostics go to stderr)",
    )
    sub = p.add_subparsers(dest="command", metavar="COMMAND")

    def add(name: str, handler: Handler, help_: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help_, description=help_, parents=[_GLOBALS])
        sp.set_defaults(handler=handler)
        return sp

    sp = add("init", cmd.init, "write a commented overlay skeleton and config file")
    sp.add_argument("--dir", default=".", help="directory to write into (default .)")
    sp.add_argument("--warehouse", default=None, help="warehouse name for the overlay")
    sp.add_argument("--force", action="store_true", help="overwrite existing files")

    sp = add("crawl", cmd.crawl, "index the catalog (metadata only)")
    sp.add_argument("--include", action="append", default=None, help="glob to include")
    sp.add_argument("--exclude", action="append", default=None, help="glob to exclude")

    sp = add("measure", cmd.measure, "measure tables (default: drain the queue)")
    sp.add_argument("targets", nargs="*", help="table names or globs")
    sp.add_argument("--all", action="store_true", help="measure every live table, hottest first")
    sp.add_argument("--max-bytes", type=int, default=None, help="total byte budget for this run")
    sp.add_argument("--max-probes", type=int, default=None, help="probe budget for this run")
    sp.add_argument("--max-seconds", type=float, default=None, help="wall-clock budget")

    sp = add("draft", cmd.draft, "draft table meaning with an LLM")
    sp.add_argument("targets", nargs="+", help="table names or globs")
    sp.add_argument("--provider", choices=("anthropic", "openai"), default="anthropic")
    sp.add_argument("--model", default=None, help="model id (default: config llm_model)")
    sp.add_argument("--budget-tokens", type=int, default=None, help="token cap for this run")
    sp.add_argument("--no-verify", action="store_true", help="skip checking drafted claims")

    sp = add("review", cmd.review, "walk drafts: y(es) n(o) e(dit) s(kip) q(uit)")
    sp.add_argument("--table", default=None, help="only drafts for this table")
    sp.add_argument("--write-overlay", default=None, help="also append accepted facts here")

    sp = add("accept", cmd.accept, "accept drafts by fact id")
    sp.add_argument("ids", nargs="+", help="drafted fact ids")
    sp.add_argument("--write-overlay", default=None, help="also append accepted facts here")

    sp = add("export-drafts", cmd.export_drafts, "write drafts to a reviewable overlay file")
    sp.add_argument("path", nargs="?", default="schemaguru/drafts.yaml")

    sp = add("import-dbt", cmd.import_dbt, "import descriptions and tests from a dbt manifest")
    sp.add_argument("manifest", help="path to target/manifest.json")

    sp = add("context", cmd.context, "print the question-scoped view an agent reads")
    sp.add_argument("question")
    sp.add_argument("--budget-tokens", type=int, default=4000)
    sp.add_argument("--profile", default="agent")

    sp = add("search", cmd.search, "ranked tables for a text query")
    sp.add_argument("query")
    sp.add_argument("-k", type=int, default=10)

    sp = add("path", cmd.path, "measured join routes between two tables (1 if none)")
    sp.add_argument("a")
    sp.add_argument("b")

    sp = add("refresh", cmd.refresh, "recrawl, re-measure, classify changes (exit code = report)")
    sp.add_argument("--label", default=None, help="snapshot label")

    add("report", cmd.report, "summarise the store: tables, facts, spend, feedback")

    sp = add("lint", cmd.lint, "load overlays and report problems (exit 1 on findings)")
    sp.add_argument("paths", nargs="*", help="overlay files/directories (default schemaguru*)")

    sp = add("serve", cmd.serve, "run the HTTP server (schemaguru[server])")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8080)

    add("mcp", cmd.mcp, "run the MCP server on stdio (schemaguru[mcp])")

    add("doctor", cmd.doctor, "check connectivity, FTS5, FactLang, extras, budgets")

    sp = add("worker", cmd.worker, "drain the measurement queue in a loop")
    sp.add_argument("--interval", type=float, default=30.0, help="seconds between drains")
    sp.add_argument("--once", action="store_true", help="drain once and exit")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI; returns the exit code (never raises for user errors)."""
    parser = _parser()
    try:
        args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    except SystemExit as e:  # argparse: 0 for --help/--version, 2 for bad usage
        return e.code if isinstance(e.code, int) else EXIT_INVALID
    handler: Handler | None = getattr(args, "handler", None)
    io = cmd.IO(args.format)
    if handler is None:
        parser.print_usage(sys.stderr)
        io.error("a command is required (see schemaguru --help)")
        return EXIT_INVALID
    try:
        return handler(args, io)
    except cmd.UsageError as e:
        io.error(str(e))
        return EXIT_INVALID
    except ConfigError as e:
        io.error(f"invalid configuration: {e}")
        return EXIT_INVALID
    except KeyboardInterrupt:
        io.error("interrupted")
        return 130
    except SchemaGuruError as e:
        io.error(str(e))
        return EXIT_ERROR
    except OSError as e:
        io.error(f"{type(e).__name__}: {e}")
        return EXIT_ERROR
    except Exception as e:  # a driver error must not print a traceback holding a URL
        io.error(f"{type(e).__name__}: {_redact(str(e))}")
        return EXIT_ERROR
