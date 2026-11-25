# FAQ

> Short answers to the questions people ask first: does it need an LLM, does it cost money, why does `measure` say SG304, how is it different from a data catalog.
> Each answer links to the page with the details.
> If your question is not here, open an issue on GitHub.

### Does SchemaGuru need an LLM?

No. Crawling, measuring, the join graph, scoping, rendering and the agent tools all work with zero
LLM calls. An LLM only adds *drafted* descriptions, use cases and pitfalls ([[Drafting]]), which
you can also write by hand in overlays ([[Overlays]]).

### Will it cost me money?

Only when you ask. A crawl reads metadata (billed on BigQuery at 10 MiB per query minimum).
Measurement runs only when you call `measure()`, `refresh()`, `schemaguru worker`, or opt into a
background/block mode, and always inside a byte budget (1 GiB per probe and 10 GiB per run by
default). An agent asking questions spends nothing. The one agent tool that would spend,
`check_fact`, is off unless you set `Config(agent_checks="allow")`, and is budgeted then. See
[[Scale-and-Cost]].

### `measure` says SG304. What is wrong?

The installed FactLang lacks `Checker.measure` (SchemaGuru needs FactLang 0.1.1 or later), or
the Guru has no connector. Run `schemaguru doctor` to see which capabilities are present.
Meanwhile crawl, overlays, scoping, rendering and the agent tools work; join paths need
measured joins, so `find_path` answers `NO_EVIDENCE` until then. See [[Measurement]].

### Does it run my agent's SQL?

No. Running queries is your host's job. SchemaGuru gives the agent knowledge, an optional
read-only guard pre-check (`sg.guard.check_sql`) and a way to report failed queries back
(`guru.report_error`). See [[Feedback-and-Guard]].

### Does it write to my warehouse?

Never. It reads system views and runs read-only probes. Give it a read-only role regardless
([[Security]]).

### How is this different from a data catalog or dbt docs?

A catalog stores what people wrote. SchemaGuru *measures* (join match in both directions,
uniqueness, real enum values, freshness), derives safe join paths with a cartesian guard, labels
every statement with its provenance, and serves a small question-scoped view to an agent. It can
import dbt descriptions and tests as declared expectations and then check them ([[Overlays]]).

### Why did the agent's view say "unmeasured"?

The table was crawled but never measured. The question queued it; drain the queue with
`guru.measure()` or `schemaguru worker`. See [[Scoping]] ("First touch").

### Why does my question find no tables?

Lexical retrieval matches words in table and column names, comments, descriptions, use cases and
glossary terms. If none of them contain the question's words, nothing matches. Add descriptions
or use cases (overlays, or drafting), add glossary terms, or try the embedding retriever. See
[[Scoping]].

### Why was my join refused?

`find_path` says why: `NO_EVIDENCE` (not measured, or broken), `NO_ROUTE`, `HUB_PIVOT` (a
cartesian hazard), or a waypoint rule. A join matching under 5% is broken and refused; a thin but
real join is offered, labelled `provisional`. See [[Path-Search-and-Hub-Guard]].

### Can I trust a number marked ≈?

It is an estimate from a sample of a large table, with its sample fraction and interval. It is
never marked verified and never compared with exact numbers. `guru.verify(facts, exact=True)`
re-measures exactly within budget. See [[Sampling]].

### Will my customers' e-mail addresses reach the LLM?

Not in the default `masked` mode: they become `<email>`. `raw` needs an explicit per-table opt-in;
`none` sends no values. Masking also applies to views and tool results. Its heuristics have
limits (lower-case names, for example), listed on [[PII-and-Sampling-Policy]].

### Can people correct what SchemaGuru says?

Yes, in YAML overlays, which always beat LLM drafts. Expectations (declared values, keys, joins,
soft-delete rules) are checked against the data and contradictions are shown; SchemaGuru never
changes your file. See [[Overlays]].

### Which warehouses are supported?

BigQuery, Snowflake, Redshift, Databricks, PostgreSQL, DuckDB, ClickHouse, SQL Server / Synapse,
Oracle, Trino and MySQL/MariaDB. The container-testable ones are validated against real engines
in CI; the cloud ones (and Oracle) are built from documented system views and not yet validated
against live accounts. Any other warehouse: write a `CatalogReader` ([[Extending-CatalogReader]]).

### Is there a server?

Yes, optionally: `schemaguru serve` (extra `server`) exposes the same operations over HTTP with
bearer tokens, and replicas can share a Postgres store ([[Server]]). You do not need it: the
library and the stdio MCP server (`schemaguru mcp`, [[Agent-Integration]]) work without one.

### Which Python versions?

3.10 to 3.13, on Linux, macOS and Windows (all tested in CI).

## See also

- [[Home]]
- [[Glossary]]
- [[Concepts]]
- [[Diagnostics]]
