# Diagnostics

> SchemaGuru reports problems as *diagnostics* instead of raising: a code like `SG301`, a severity, a message, and where possible a location and a hint.
> One bad item costs one item: a failed probe, overlay entry or draft becomes a diagnostic and the rest of the run continues.
> Codes are grouped by component (SG1xx catalog ... SG8xx agent), and the table below is generated from the source, so it is complete.

## The `Diagnostic` object

```python
sg.Diagnostic(code="SG203", severity=Severity.ERROR,
              message="cannot resolve table 'shop.orders_backup_2019' in the store",
              location="tests/fixtures/overlays/spec_example.yaml:36", hint=None)
```

(Real diagnostic from loading the spec example overlay into the shop store, 2026-11-24.)

| Field | Meaning |
|---|---|
| `code` | `SGnnn`, stable across releases |
| `severity` | `ERROR`, `WARNING` or `INFO` |
| `message` | plain language; values are masked and URLs redacted |
| `location` | `file:line` for overlays, a table or column ref, a chain name... |
| `hint` | what to do about it, when there is something to do |

`d.to_dict()` gives JSON. The CLI prints diagnostics to stderr as
`error SG304: <message> (hint: ...)`.

## Where you find them

| Where | Holds |
|---|---|
| `guru.load_overlays(...)` | returns that load's diagnostics |
| `guru.diagnostics` | every distinct diagnostic the façade has seen (overlays, rules, chains, wiring) |
| `CrawlReport.errors` | skipped catalog items (`SG102`) |
| `MeasureReport.errors` | probe failures and budget stops |
| `DraftReport.errors`, `.diagnostics` | drafting problems |
| `ContextView.diagnostics` | rendering notes (`SG50x`) |
| `store.diagnostics` | store notes such as `SG701` |
| agent tool results | `{"error": {"code": "SG803", "message": ...}, "text": ...}` |

## Errors that are raised

Library code raises only for programmer errors (bad arguments) and unrecoverable store errors.
All deliberate exceptions derive from `sg.SchemaGuruError`:

- `ConfigError`: an unknown config key or a value outside its allowed set;
- `AmbiguousNameError`: a bare name matches several objects (`.candidates` lists them);
- `StoreVersionError`: the store was written by a newer schema version (`SG702`);
- `StoreUnavailableError`: a shared (Postgres) store cannot be reached right now (`SG703`);
- `StoreConfigError` (a `ConfigError`): the shared store rejected the configured credentials
  (`SG704`); retrying does not help;
- `CatalogError`: a catalog-layer failure with an `SG1xx` code (e.g. `SG106` for a URL that
  cannot be opened);
- `SchemaGuruError` itself, e.g. "unknown table 'nosuch' (crawl first, or qualify the name)".

## All codes

Generated from `schemaguru.diagnostics.CODES` (the repository's
[`docs/reference/diagnostics.md`](https://github.com/rupakc/schemaguru/blob/main/docs/reference/diagnostics.md)
holds the same list, and a test keeps that file equal to the codes the source emits).

### SG1xx: Connection and catalog

| Code | Meaning |
|---|---|
| `SG101` | no catalog reader is registered for the requested dialect |
| `SG102` | the crawl skipped a catalog, schema, page or batch that failed to read |
| `SG104` | metadata SQL was refused: only a single SELECT/WITH/SHOW/DESCRIBE is allowed |
| `SG105` | the installed FactLang does not provide the metadata-SQL capability |
| `SG106` | a connection URL could not be opened (no FactLang connect, no built-in driver) |
| `SG107` | the object given as a connector is not one SchemaGuru can use, or its dialect cannot be told (ConfigError naming the accepted types) |
| `SG108` | an installed plugin (entry point schemaguru.<point>) failed to load and was skipped |
| `SG109` | a value cannot be inlined for a host without bind parameters (a backslash, NUL, non-scalar or non-finite value); the probe is refused |

### SG2xx: Overlays

| Code | Meaning |
|---|---|
| `SG201` | overlay 'version' is missing or unsupported |
| `SG202` | overlay has an unknown key (a close match is suggested) |
| `SG203` | overlay reference does not resolve to anything in the store |
| `SG204` | overlay reference is ambiguous; the candidates are listed |
| `SG205` | overlay filter text is invalid |
| `SG206` | overlay waiver has no valid 'expires' date and is ignored |
| `SG207` | overlay waiver has expired and is ignored |
| `SG208` | overlay file is unreadable or an entry is malformed |
| `SG209` | dbt manifest is unreadable |
| `SG210` | a declared soft-delete filter does not match the catalog or the measured values; it is shown with a warning and never applied in SQL snippets |

### SG3xx: Measurement and budget

| Code | Meaning |
|---|---|
| `SG301` | probe over the per-probe byte budget was not run (fact ERROR over_budget) |
| `SG302` | total measurement budget exhausted; the remaining tables stay queued |
| `SG303` | a measurement probe failed (fact ERROR for that subject only) |
| `SG304` | the installed FactLang does not provide a needed measurement capability |
| `SG305` | a table asked to be measured is not in the store (crawl it first) |

### SG4xx: Drafting and LLM

| Code | Meaning |
|---|---|
| `SG401` | an LLM draft reply was invalid twice (schema or unknown names); fact ERROR |
| `SG402` | draft token budget exhausted; the remaining tables were not drafted |
| `SG403` | the LLM provider call failed; that call's tables get an ERROR draft fact |
| `SG404` | a table asked to be drafted is not in the store (crawl it first) |
| `SG405` | a drafted claim could not be checked (no verdict); the draft stays UNVERIFIED |
| `SG406` | a drafted soft-delete predicate is not a safe filter; it is never checked |

### SG5xx: Scoping and rendering

| Code | Meaning |
|---|---|
| `SG501` | a join snippet was not rendered (FactLang lacks render_join or refused the path) |
| `SG502` | filter text is invalid (FactLang parse_filter, or the syntax-only fallback) |
| `SG503` | filter text was checked for syntax only: FactLang lacks parse_filter |
| `SG504` | a context view exceeds its token budget: the always-on blocks do not fit |
| `SG505` | info: how many values or text spans the value policy masked in a view |
| `SG507` | a `sampling.per_glob` entry matches no stored table (globs are case-insensitive): its value mode applies to nothing; check the glob for a typo |

### SG6xx: Graph

| Code | Meaning |
|---|---|
| `SG601` | a malformed join fact was skipped; only that graph edge is lost |
| `SG602` | a waypoint rule is malformed; the searches it governs are refused |
| `SG603` | a waypoint rule has more than 16 requirements; its searches are refused |
| `SG604` | a waypoint prohibition or requirement matches no edge or table in the graph |
| `SG605` | an identity chain cannot be compiled into a waypoint rule (it is not applied) |

### SG7xx: Store

| Code | Meaning |
|---|---|
| `SG701` | SQLite FTS5 is unavailable; search falls back to a slower LIKE scan |
| `SG702` | the store was written by a newer schema version than this library supports |
| `SG703` | the shared store is unavailable (refused, lost or pool exhausted; HTTP 503) |
| `SG704` | the shared store rejected the configured credentials: a configuration error, not an outage (HTTP 500) |

### SG8xx: Agent and server

| Code | Meaning |
|---|---|
| `SG801` | guard refused the SQL: not a single read-only SELECT/WITH statement |
| `SG802` | guard refused the SQL: it reads a never-query table |
| `SG803` | an agent tool call was refused: unknown tool or invalid arguments |
| `SG804` | an agent tool failed unexpectedly (error type and a masked message returned) |
| `SG805` | server: missing or invalid bearer token (HTTP 401) |
| `SG806` | server: the route needs an admin token (HTTP 403) |
| `SG807` | server: invalid request (bad JSON, missing or wrong-typed parameter; HTTP 400) |
| `SG808` | server: the table or column named in the request is not in the store (HTTP 404) |
| `SG809` | server: a refresh is already running on this store (HTTP 409) |
| `SG810` | server: unexpected internal error; details are logged, not returned (HTTP 500) |
| `SG811` | server: no usable bearer tokens configured (missing, too short, or unknown role) |
| `SG812` | server: MCP over streamable HTTP is unavailable in this build (HTTP 501) |

## Common ones and what to do

| You see | Usually means | Do |
|---|---|---|
| `SG304` | FactLang lacks `Checker.measure` (before FactLang 0.1.0) or no connector | upgrade FactLang; meanwhile overlays, scoping and rendering work |
| `SG301` | one probe is estimated over `max_bytes_per_probe` | raise the cap for that run, or let [[Sampling]] apply |
| `SG302` | the run's byte total is spent | the rest is queued; run again later or raise `max_bytes_total` |
| `SG203` | an overlay names a table the store does not have | crawl first, check the name, or remove the entry |
| `SG604` | a waypoint predicate matches nothing in the graph | a typo, or the join is not measured yet |
| `SG605` | a chain could not be compiled | the chain's join is not measured, or the chain is not linear |
| `SG501` / `SG503` | FactLang lacks join rendering / filter parsing | informational; snippets are omitted / filters are syntax-checked only |
| `SG701` | SQLite has no FTS5 | search still works, slower; use a Python build with FTS5 |
| `SG803` | an agent tool call was refused | the message says why; show it to the agent |
| `SG805`-`SG812` | HTTP server errors | see the status table on [[Server]] |

## See also

- [[CLI]] - exit codes
- [[FAQ]] - "why does measure say SG304?"
- [[Overlays]] - the SG2xx codes in context
- [[Security]] - why messages are masked
