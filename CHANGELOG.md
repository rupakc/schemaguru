# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/) (in `0.x` the public API is stable within a minor).

## [Unreleased]

## [0.1.0] - 2026-11-25

First release.

### Added
- `Guru` facade with async twins: `connect(url)`, or any connection a host already holds
  (DB-API connection, SQLAlchemy engine, BigQuery client, `execute(sql)` callable); `crawl`,
  `measure`, `draft`, `accept`, `load_overlays`, `context`, `find_path`, `refresh`,
  `report_error`, `export`, `fingerprint_views`, `tools`, `handle_tool_call`, `prompt_block`.
- Catalog readers for DuckDB, PostgreSQL, MySQL/MariaDB, ClickHouse, SQL Server/Synapse and
  Trino (tested against real engines) and for BigQuery, Snowflake, Redshift, Databricks and
  Oracle (tested on recorded metadata; not yet validated against live accounts). Crawls are
  metadata-only, paged, resumable and incremental, with include/exclude globs and a cap on
  billed metadata bytes (`Budget.crawl_bytes`, warning `SG110`).
- Knowledge stores: SQLite (default) and Postgres (extra `postgres-store`, shared by teams and
  server replicas; `store="postgresql://..."` opens it), with migrations, full-text search and
  byte-deterministic JSON export.
- Measurement through FactLang (`factlang>=0.1.2,<0.2`): row counts, column profiles,
  uniqueness (composite keys in full), observed values, join match in both directions,
  fan-out, freshness and pattern rates; per-probe, per-run and per-period byte budgets with a
  ledger; representative key-hash / block sampling of large tables, labelled `ESTIMATED`;
  first-touch queueing with queue, background or block modes.
- Hosts without bind parameters (`execute(sql)`/`query(sql)`) get values inlined as literals
  in FactLang 0.1.2's dialect spellings (T-SQL `N'…'` text, ISO casts and `1`/`0` booleans,
  also for its aliases `mssql`, `sqlserver`, `synapse`, …; Oracle `1`/`0` and
  `TIMESTAMP 'YYYY-MM-DD HH:MM:SS'`), each re-checked against exactly that form. `SG109`
  refuses a backslash, NUL or line terminator (CR, LF, U+0085, U+2028, U+2029), a `'` on any
  dialect without a doubled-quote form (BigQuery, Databricks, Spark, Hive, unknown dialects),
  and anything FactLang declines to inline (`FL507`, `FL500`).
- Join graph from measured and declared edges: additive cost per hop, k-shortest path search,
  thin joins labelled, broken joins refused, hub-pivot refusal, waypoint rules and identity
  chains, with dialect-correct join snippets.
- Human YAML overlays (descriptions, enums kept as expectations, soft-delete filters, joins,
  glossary, waivers that expire) and dbt manifest import; humans always win over drafts.
- LLM drafting with Anthropic and OpenAI-compatible providers, verified against measured
  facts, reviewed and accepted into overlays.
- PII and sampling policy: `masked` (default), `raw` (opt-in) and `none`, per table glob
  (case-insensitive); shape detectors, masking, an outbound sweep of every LLM and agent
  payload, and credential scrubbing of every error message.
- Scoping and rendering: question-scoped, token-budgeted views per role in text, Markdown and
  JSON; consequence-ordered, byte-deterministic and fingerprinted.
- Refresh and drift: re-crawl, re-measure, diff against the previous snapshot and classify
  each change; runtime error feedback and a read-only SQL guard.
- Agent surface: JSON tool specs, a prompt block, an MCP server over stdio and streamable
  HTTP, and LangChain, LlamaIndex, OpenAI Agents and Anthropic adapters.
- Optional HTTP server (`schemaguru serve`): `/context`, `/search`, `/tables`, `/paths`,
  `/facts`, `/refresh` and `/mcp`, bearer tokens, one refresh at a time across replicas.
- The `schemaguru` command line for every operation, with `--format json` output (errors
  included).
- Plugins through entry points (readers, fact kinds, detectors, profiles and formats,
  classifiers, join candidate sources, stores, retrievers, embedders, token counters, LLM
  providers, authenticators); a documentation site, a wiki and an interactive demo.

[Unreleased]: https://github.com/rupakc/schemaguru/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/rupakc/schemaguru/releases/tag/v0.1.0
