# Diagnostic codes

Generated from `schemaguru.diagnostics.CODES`; a test asserts the two agree.

| Code | Meaning |
|---|---|
| SG101 | no catalog reader is registered for the requested dialect |
| SG102 | the crawl skipped a catalog, schema, page or batch that failed to read |
| SG104 | metadata SQL was refused: only a single SELECT/WITH/SHOW/DESCRIBE is allowed |
| SG105 | the installed FactLang does not provide the metadata-SQL capability |
| SG106 | a connection URL could not be opened (no FactLang connect, no built-in driver) |
| SG107 | the object given as a connector is not one SchemaGuru can use, or its dialect cannot be told (ConfigError naming the accepted types) |
| SG108 | an installed plugin (entry point schemaguru.<point>) failed to load and was skipped |
| SG201 | overlay 'version' is missing or unsupported |
| SG202 | overlay has an unknown key (a close match is suggested) |
| SG203 | overlay reference does not resolve to anything in the store |
| SG204 | overlay reference is ambiguous; the candidates are listed |
| SG205 | overlay filter text is invalid |
| SG206 | overlay waiver has no valid 'expires' date and is ignored |
| SG207 | overlay waiver has expired and is ignored |
| SG208 | overlay file is unreadable or an entry is malformed |
| SG209 | dbt manifest is unreadable |
| SG210 | a declared soft-delete filter does not match the catalog or the measured values; it is shown with a warning and never applied in SQL snippets |
| SG301 | probe over the per-probe byte budget was not run (fact ERROR over_budget) |
| SG302 | total measurement budget exhausted; the remaining tables stay queued |
| SG303 | a measurement probe failed (fact ERROR for that subject only) |
| SG304 | the installed FactLang does not provide a needed measurement capability |
| SG305 | a table asked to be measured is not in the store (crawl it first) |
| SG401 | an LLM draft reply was invalid twice (schema or unknown names); fact ERROR |
| SG402 | draft token budget exhausted; the remaining tables were not drafted |
| SG403 | the LLM provider call failed; that call's tables get an ERROR draft fact |
| SG404 | a table asked to be drafted is not in the store (crawl it first) |
| SG405 | a drafted claim could not be checked (no verdict); the draft stays UNVERIFIED |
| SG406 | a drafted soft-delete predicate is not a safe filter; it is never checked |
| SG501 | a join snippet was not rendered (FactLang lacks render_join or refused the path) |
| SG502 | filter text is invalid (FactLang parse_filter, or the syntax-only fallback) |
| SG503 | filter text was checked for syntax only: FactLang lacks parse_filter |
| SG504 | a context view exceeds its token budget: the always-on blocks do not fit |
| SG505 | info: how many values or text spans the value policy masked in a view |
| SG506 | a filter names a column matching several catalog columns that differ only in case: left unquoted |
| SG601 | a malformed join fact was skipped; only that graph edge is lost |
| SG602 | a waypoint rule is malformed; the searches it governs are refused |
| SG603 | a waypoint rule has more than 16 requirements; its searches are refused |
| SG604 | a waypoint prohibition or requirement matches no edge or table in the graph |
| SG605 | an identity chain cannot be compiled into a waypoint rule (it is not applied) |
| SG701 | SQLite FTS5 is unavailable; search falls back to a slower LIKE scan |
| SG702 | the store was written by a newer schema version than this library supports |
| SG703 | the shared store is unavailable (refused, lost or pool exhausted; HTTP 503) |
| SG704 | the shared store rejected the configured credentials: a configuration error, not an outage (HTTP 500) |
| SG801 | guard refused the SQL: not a single read-only SELECT/WITH statement |
| SG802 | guard refused the SQL: it reads a never-query table |
| SG803 | an agent tool call was refused: unknown tool or invalid arguments |
| SG804 | an agent tool failed unexpectedly (error type and a masked message returned) |
| SG805 | server: missing or invalid bearer token (HTTP 401) |
| SG806 | server: the route needs an admin token (HTTP 403) |
| SG807 | server: invalid request (bad JSON, missing or wrong-typed parameter; HTTP 400) |
| SG808 | server: the table or column named in the request is not in the store (HTTP 404) |
| SG809 | server: a refresh is already running on this store (HTTP 409) |
| SG810 | server: unexpected internal error; details are logged, not returned (HTTP 500) |
| SG811 | server: no usable bearer tokens configured (missing, too short, or unknown role) |
| SG812 | server: MCP over streamable HTTP is unavailable in this build (HTTP 501) |
