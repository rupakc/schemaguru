# Diagnostic codes

Generated from `schemaguru.diagnostics.CODES`; a test asserts the two agree.

| Code | Meaning |
|---|---|
| SG101 | no catalog reader is registered for the requested dialect |
| SG102 | the crawl skipped a catalog, schema, page or batch that failed to read |
| SG104 | metadata SQL was refused: only a single SELECT/WITH/SHOW/DESCRIBE is allowed |
| SG105 | the installed FactLang does not provide the metadata-SQL capability |
| SG106 | a connection URL could not be opened (no FactLang connect, no built-in driver) |
| SG201 | overlay 'version' is missing or unsupported |
| SG202 | overlay has an unknown key (a close match is suggested) |
| SG203 | overlay reference does not resolve to anything in the store |
| SG204 | overlay reference is ambiguous; the candidates are listed |
| SG205 | overlay filter text is invalid |
| SG206 | overlay waiver has no valid 'expires' date and is ignored |
| SG207 | overlay waiver has expired and is ignored |
| SG208 | overlay file is unreadable or an entry is malformed |
| SG209 | dbt manifest is unreadable |
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
| SG601 | a malformed join fact was skipped; only that graph edge is lost |
| SG602 | a waypoint rule is malformed; the searches it governs are refused |
| SG603 | a waypoint rule has more than 16 requirements; its searches are refused |
| SG604 | a waypoint prohibition or requirement matches no edge or table in the graph |
| SG605 | an identity chain cannot be compiled into a waypoint rule (it is not applied) |
| SG701 | SQLite FTS5 is unavailable; search falls back to a slower LIKE scan |
| SG702 | the store was written by a newer schema version than this library supports |
| SG801 | guard refused the SQL: not a single read-only SELECT/WITH statement |
| SG802 | guard refused the SQL: it reads a never-query table |
| SG803 | an agent tool call was refused: unknown tool or invalid arguments |
