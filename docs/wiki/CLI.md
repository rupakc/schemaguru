# CLI

> The `schemaguru` command does everything the Python API does from a shell or a CI job: init, crawl, measure, draft, review, context, search, path, refresh, lint, report, doctor, MCP and a measurement worker.
> It reads the warehouse URL from `--url` or `$SCHEMAGURU_URL`, the store from `--store` (default `.schemaguru/default.db`), and config from `--config` or `SCHEMAGURU_*` environment variables.
> Exit codes are meant for CI: 0 ok, 1 findings that need review, 2 invalid input, 3 errors or nothing measured.

## Help

Real output (SchemaGuru 0.1.0):

```text
$ schemaguru --help
usage: schemaguru [-h] [--version] [--url URL] [--store STORE]
                  [--config CONFIG] [--format {text,json,markdown}]
                  COMMAND ...

Automated schema intelligence for data warehouses.

positional arguments:
  COMMAND
    init                write a commented overlay skeleton and config file
    crawl               index the catalog (metadata only)
    measure             measure tables (default: drain the queue)
    draft               draft table meaning with an LLM
    review              walk drafts: y(es) n(o) e(dit) s(kip) q(uit)
    accept              accept drafts by fact id
    export-drafts       write drafts to a reviewable overlay file
    import-dbt          import descriptions and tests from a dbt manifest
    context             print the question-scoped view an agent reads
    search              ranked tables for a text query
    path                measured join routes between two tables (1 if none)
    refresh             recrawl, re-measure, classify changes (exit code =
                        report)
    report              summarise the store: tables, facts, spend, feedback
    lint                load overlays and report problems (exit 1 on findings)
    serve               run the HTTP server (schemaguru[server])
    mcp                 run the MCP server on stdio (schemaguru[mcp])
    doctor              check connectivity, FTS5, FactLang, extras, budgets
    worker              drain the measurement queue in a loop

options:
  -h, --help            show this help message and exit
  --version             show program's version number and exit
  --url URL             warehouse URL (or $SCHEMAGURU_URL)
  --store STORE         store path (default .schemaguru/default.db)
  --config CONFIG       a schemaguru.config.yaml (else environment)
  --format {text,json,markdown}
                        output format (json: parseable on stdout; diagnostics
                        go to stderr)
[exit 0]
```

`python -m schemaguru` works the same way.

## Global options

These work before or after the command name.

| Option | Meaning |
|---|---|
| `--url URL` | warehouse URL; else `$SCHEMAGURU_URL`. Commands that only read the store (`context`, `search`, `path`, `report`, `review`, `accept`) work without it once a store exists |
| `--store PATH` | store file (default `Config.store_path`, else `.schemaguru/default.db`), or a `postgres://` DSN for the shared [[Postgres store|Store]] (extra `postgres-store`) |
| `--config FILE` | a `schemaguru.config.yaml`; without it, `SCHEMAGURU_*` environment variables are read ([[Configuration]]) |
| `--format text\|json\|markdown` | output format; with `json`, stdout is parseable and diagnostics go to stderr |

URLs are never printed in full: messages show only `scheme://host[:port]`.

## Commands

| Command | What it does | Exit codes |
|---|---|---|
| `init [--dir D] [--warehouse W] [--force]` | write a commented `schemaguru.yaml` overlay skeleton and `schemaguru.config.yaml` | 0; 2 if the files exist (use `--force`) |
| `crawl [--include G]... [--exclude G]...` | index the catalog (metadata only) | 0; 3 on errors |
| `measure [TABLES...] [--all] [--max-bytes N] [--max-probes N] [--max-seconds S]` | measure tables (default: drain the queue); budget flags override this run | 0; 3 on errors or when explicitly asked tables measured nothing |
| `draft TABLES... [--provider anthropic\|openai] [--model M] [--budget-tokens N] [--no-verify]` | draft meaning with an LLM ([[Drafting]]) | 0; 3 when nothing was drafted and there were errors |
| `review [--table T] [--write-overlay F]` | walk drafts interactively: y(es) n(o) e(dit) s(kip) q(uit) | 0 |
| `accept IDS... [--write-overlay F]` | accept drafts by fact id | 0 |
| `export-drafts [PATH]` | write drafts to a reviewable YAML file (default `schemaguru/drafts.yaml`) | 0 |
| `import-dbt MANIFEST` | import descriptions and tests from `target/manifest.json` | 0; 2 if the manifest cannot be read |
| `context "QUESTION" [--budget-tokens N] [--profile P]` | print the view an agent reads | 0 |
| `search QUERY [-k N]` | ranked tables | 0 |
| `path A B` | join routes between two tables | 0; **1 when refused** |
| `refresh [--label L]` | recrawl, re-measure, classify changes ([[Refresh-and-Drift]]) | the report's: 0, 1 or 3 |
| `report` | summarise the store: tables, facts, spend, feedback | 0 |
| `lint [PATHS...]` | load overlays on a copy of the store and report problems | 0; 1 on any finding |
| `serve [--host H] [--port P]` | the HTTP server ([[Server]]; tokens from `SCHEMAGURU_SERVER_TOKENS`) | 0 when stopped; 2 without tokens or without the `server` extra |
| `mcp` | the MCP server on stdio ([[Agent-Integration]]) | 0 |
| `doctor` | check connectivity, FTS5, FactLang, extras, budgets | 0; 3 on a FAIL (WARNs pass) |
| `worker [--interval S] [--once]` | drain the measurement queue in a loop (default every 30 s) | 0; 3 after errors |

Invalid arguments or configuration exit 2; Ctrl-C exits 130.

## A session on the shop fixture

Real output, generated 2026-11-24 in a directory holding the shop fixture as
`warehouse.duckdb` and the spec example overlay as `schemaguru.yaml`, with the FactLang installed
at the time (no measurement capability yet):

```text
$ schemaguru crawl --url duckdb:///warehouse.duckdb
tables seen 14, written 14, withdrawn 0
6 catalog queries in 0.0s
[exit 0]

$ schemaguru search refunds
no matching tables
[exit 0]

$ schemaguru measure --url duckdb:///warehouse.duckdb shop.orders
error SG304: the measurement backend cannot measure (the installed FactLang lacks Checker.measure, or no connector was given); nothing was measured (hint: pass backend=..., or upgrade FactLang)
measured 0 table(s), 0 fact(s), 0 bytes
[exit 3]

$ schemaguru path shop.orders shop.customers
no usable measured join touches shop.orders, shop.customers (broken joins are refused; unmeasured candidates need include_unmeasured)
[exit 1]

$ schemaguru report
14 live table(s), 0 withdrawn
0 fact(s); 0 draft(s) awaiting review
latest snapshot: None
spend: measure_bytes 0, measure_queries 0, draft_tokens 0, crawl_queries 6
0 failing-query pattern(s) reported
[exit 0]

$ schemaguru doctor --url duckdb:///warehouse.duckdb
PASS connectivity: connected to duckdb:// (duckdb); 2 schema(s) readable
PASS fts5: SQLite FTS5 is available
WARN factlang: no FactLang capability detected: measurement reports SG304
     hint: pip install -U 'factlang>=0.1.2,<0.2'
PASS extras: installed: anthropic, clickhouse, duckdb, embeddings, mcp, mssql, mysql, openai, postgres, server, tiktoken, trino
PASS budgets: per probe 1073741824 bytes, total 10737418240 bytes
[exit 0]

$ schemaguru --format json search refunds
{
  "tables": [],
  "text": "no matching tables"
}
[exit 0]
```

What this shows: the crawl works without FactLang's measurement; `search refunds` finds nothing
because nothing yet describes refunds ([[Scoping]]); `measure` refuses honestly with `SG304`
(exit 3); `path` has no measured joins to use (exit 1); `doctor` passes with a WARN for the
missing FactLang capability. `doctor` reports a missing FactLang capability as WARN, not FAIL.

## Using it in CI

```bash
export SCHEMAGURU_URL="snowflake://..."                     # from a secret
schemaguru lint                                             # overlays valid? exit 1 if not
schemaguru refresh --format markdown > refresh.md           # exit 1 if review-class drift
```

Budgets for a CI refresh come from the config file or the environment, e.g.
`SCHEMAGURU_BUDGET__MAX_BYTES_TOTAL=5368709120`.

## See also

- [[Configuration]] - config file and environment variables
- [[Diagnostics]] - the codes in CLI messages
- [[Refresh-and-Drift]] - the refresh report
- [[Overlays]] - `init`, `lint`, `import-dbt`
