# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/) (in `0.x` the public API is stable within a minor).

## [Unreleased]

## [0.2.1] - 2026-11-27

No library change: the API, the CLI and the store are those of 0.2.0.

### Documentation
- A redesigned [documentation site](https://rupakc.github.io/schemaguru/): a landing page, a
  features page, and an in-browser playground (the real library under Pyodide, built from the same
  commit by the docs workflow) that replaces the interactive demo.
- The SchemaGuru mascot, a meditating guru pondering a schema, as the logo, the site icon and the
  link-preview card, and at the top of the README (so on PyPI too).

### Fixed
- The nightly scale benchmark failed on its documented `context_p95_ms` limit: the comparator now
  also reads `known_limits` from committed results at the same table count, and the first GitHub
  runner result is committed as that runner's regression baseline.

## [0.2.0] - 2026-11-26

### Added
- **Lineage** ([guide](docs/guide/lineage.md)): two fact kinds, `LINEAGE` (table) and
  `COLUMN_LINEAGE` (column), always `UNVERIFIED`, from three sources: dbt manifests
  (`depends_on`, and `compiled_code` for columns), view definitions read by the crawl, and
  human overlays (`lineage:` entries with `upstream`, `inputs`, `transform` and `ignore`; a human
  entry beats a parsed link). Parsing needs the new `lineage` extra (`pip install
  'schemaguru[lineage]'`, sqlglot) and runs in a reused worker process with limits (definitions
  over 1,000,000 characters are skipped, 5 s per parser request). Only a sha256 and a
  literal-free template of a definition are stored.
- **The `lineage` tool** in Python (`guru.lineage(ref, direction, depth, level)` returning a
  `LineageResult`, async twin `alineage`), the CLI (`schemaguru lineage`), the HTTP server
  (`GET /lineage`) and MCP / framework adapters (through `guru.tools()`). The `agent`, `planner`
  and `verifier` views carry lineage lines. It is never a join path and never verified.
- **`view_definitions`** on `CatalogReader` (`ReaderCapabilities.view_definitions`), implemented
  by all eleven shipped readers from metadata views only; a custom reader without it is unchanged.
- New diagnostics `SG211` to `SG215` and `SG705`; store migrations for the lineage index
  (SQLite 3, Postgres 2) and for a counter of inserted `JOIN` facts that lets the context
  renderer skip an index rebuild (SQLite 4, Postgres 3), all applied on open.
- **Scale benchmark** at 500,000 tables and 25,000,000 columns: harness in `benchmarks/`, committed
  results `benchmarks/results/0.2.0.json`, a nightly regression workflow, and the
  [Scale page](docs/guide/scale.md).

### Changed
- Requires FactLang 0.1.4 (`factlang>=0.1.4,<0.2`); Databricks inline timestamps are accepted
  (FactLang now spells them with an explicit UTC zone).
- The Postgres warehouse reader reads unique-index key columns with `indnkeyatts` (PostgreSQL 11 or later); on an older server it skips unique indexes with an `SG102` warning and keeps primary and foreign keys.
- Bottlenecks the scale run found were fixed (crawl, join index, search, path search); see the
  Scale page for what moved.

### Fixed

One line per area of the 0.2.0 hardening ledger
(`docs/specs/2026-11-25-schemaguru-0.2.0-hardening-ledger.md`, which lists every item and its test).
- **PII masking:** comma-joined (`Garcia,Maria`), initial-plus-surname, lower-case, cued unlisted
  and possessive-surname names, addresses inside sentences, credentials glued to word characters,
  and `password: varchar(255)` kept; the `<secret>` and `<address>` tokens are documented.
- **Guards and inline hosts:** a file path in any `FROM` item, more file extensions, fullwidth
  separators and look-alike quotes are refused; a raising `tzinfo` is `SG109`.
- **Crawl and measurement:** resume re-checks the scope hash and crawls schemas created before the
  cursor; temp-dir cleanup; scheduler crash and queue handling; budget refill and fused-probe
  charging; sampled cardinality no longer reads as 1:1; no id-to-id join candidates.
- **Readers:** Postgres `NOT VALID` foreign keys and unique indexes, Oracle partition and
  materialized-view comments, Snowflake clustering and `SHOW` caps, BigQuery pseudo-columns,
  cross-project storage and full-key foreign-key joins, Redshift per-page schema reads.
- **Model, store and knowledge:** a stored fact id is verified on load; nested transactions use
  savepoints; search index rebuilds and read-only warnings; one JSON encoder; unknown-age and
  estimated facts go stale correctly; `diff` takes `checks_run`.
- **Overlays and rules:** waivers and duplicate keys validated, glossary `maps_to` takes tables
  only, `facts_to_overlay` names what it cannot write, changed joins replace rather than
  duplicate, atomic writes keep their mode, dbt unique counts and capped rule requirements.
- **Graph and search:** the shared graph's memo is thread-safe, hop-limit refusals are named, and
  a via-success search does bounded work.
- **Docs, CI and tests:** the BigQuery region default is documented, wiki-facts and layer-walker
  negative controls run on real pages and trees, and the denylist job fails on `main` without its
  secret.
- **Connectors:** `check_fact` honours the claim cap; the inline-connector docstring names the
  zone-explicit forms.
- **Final review:** a view whose upstream enters the crawl's scope or gains a column is re-parsed
  (the skip now also needs its names to resolve as before); `"..."` is blanked in definition
  templates for dialects where it can be a string (BigQuery, Databricks/Spark, Hive, MySQL/MariaDB,
  T-SQL, unknown); `Config.from_env` ignores `SCHEMAGURU_*` variables that are not config fields,
  so `schemaguru serve` with `SCHEMAGURU_SERVER_TOKENS` starts; the Postgres JOIN-counter trigger
  uses `EXECUTE PROCEDURE`; lineage tool errors stay readable.

### Known limits
- **`guru.context` is slow at 500,000 tables (`context_p95_ms`): p95 28.6 s (p50 9.3 s) against the 200 ms target,
  which is not met.** 87% of a call is the join-path search's exact lower bound; meeting the target
  needs an architecture change (precomputed distance bounds, or a hazard scan that does not search
  every candidate pair). At 2,000 tables p95 is 119 ms and at 20,000 tables 1.5 s. Join-path search
  p95 is 1.2 s at 500,000 tables. See the [Scale page](docs/guide/scale.md). The benchmark's join
  facts are synthetic.
- Lineage: never a join path and never verified; an unaliased computed output has no column
  lineage; the parser worker's 1 GiB cap is Linux only; a deleted `JOIN` edge can stay in
  the cached graph until the snapshot, generation or date changes
  ([details](docs/guide/lineage.md#limits)).
- Masking trades precision for recall ([PII guide](docs/guide/pii.md#known-limits)): a `00`-prefixed
  digit string is masked as a phone, a four-part version as an IP address, a place or brand name
  that shares a word with the name list as a name, and a hex segment of a path as a secret.
- Guard ([Security](docs/guide/security.md#known-limits)): a `;` inside a backtick identifier is
  refused by the metadata guard; on SQL-text-only hosts for backtick dialects a probe identifier
  containing `#`, `//` or `--` is refused (`SG104`); `literal_refusal` stays a public name.
- Overlays ([guide](docs/guide/overlays.md#known-limits)): a join entry carries only the
  cardinality you vouch for, and a malformed entry's previous facts are retracted on the next
  load.
- The shop demo's fixture declares no foreign keys in DuckDB, so the site demo shows a coincidental
  key-to-key join instead of the declared edges ([playground](docs/playground.md)).

## [0.1.1] - 2026-11-25

### Fixed
- Inline-mode hosts (SQL text only) on Trino accept timestamp parameters again: FactLang 0.1.3
  spells them `TIMESTAMP '<date> <time> UTC'`, and the literal re-check now expects that form
  (the 0.1.2 spelling is no longer accepted).

### Changed
- Requires FactLang 0.1.3 (`factlang>=0.1.3,<0.2`).
- Inline-mode hosts on Databricks refuse a timestamp parameter with an explicit `SG109` reason
  (FactLang renders it without a time zone, which Databricks reads in the session time zone)
  pending FactLang 0.1.4; a zone-explicit spelling is accepted.

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

[Unreleased]: https://github.com/rupakc/schemaguru/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/rupakc/schemaguru/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/rupakc/schemaguru/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/rupakc/schemaguru/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/rupakc/schemaguru/releases/tag/v0.1.0
