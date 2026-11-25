# Security

> SchemaGuru is read-only by design: it reads catalog metadata and runs read-only, budgeted measurement probes, and it never runs an agent's SQL.
> Data values leave it only through the value policy (masked by default), and URLs, error messages, evidence and tool results are scrubbed before anyone sees them.
> Its guard helper and never-query list are best-effort aids for cooperative agents; real isolation comes from warehouse permissions, which you must still set.

## The threat model in one table

| Concern | What SchemaGuru does | What you must do |
|---|---|---|
| writing to the warehouse | never: no DML/DDL anywhere; catalog SQL is checked to be a single read statement (`SG104`); DuckDB files are opened read-only | give SchemaGuru a **read-only role** anyway |
| spending money | lazy measurement, per-probe/run/period budgets, a ledger, no spend on the agent path | set budgets that fit your bill ([[Scale-and-Cost]]) |
| personal data reaching an LLM | `masked` by default; `raw` only on opt-in per table; `none` for no values | choose `none` for regulated tables |
| personal data in the store | masked or no values stored by default; raw stored only with `raw` **and** `store_raw_samples=True` | protect `.schemaguru/` like any metadata cache; keep it out of git |
| credentials in logs | URLs shown only as `scheme://host[:port]`; anything that does not parse cleanly becomes `scheme://***`; driver errors are scrubbed in CLI output | pass credentials by environment or secret store, not on shared command lines |
| an agent running harmful SQL | SchemaGuru never executes agent SQL; `sg.guard.check_sql` is an optional pre-check | run agent SQL under a read-only role with resource limits |
| an agent reading a forbidden table | a `never_query` table is listed in every view's never-query block and its own block is marked `⚠ NEVER QUERY: <reason>`; the guard refuses SQL that names it | **revoke the role's access**; `never_query` only steers a cooperative agent |
| prompt injection through warehouse text | warehouse comments and descriptions are passed on as text (masked by the value policy), never interpreted; agent tools accept only arguments matching their JSON schema (`SG803` otherwise) | treat warehouse text and model output as untrusted in your host |

## What is stored, and where

The SQLite store (`.schemaguru/<name>.db`) holds: catalog structure (names, types, comments,
statistics), facts (numbers, masked observed values, shape tokens, evidence with SQL
*templates* whose literals are blanked), snapshots, the queue, the spend ledger, and feedback
records (table, column, failure class and a count; **never the SQL text**). Drafting payloads
are not stored. See [[Store]].

## Where values are masked

At storage, in evidence, in error messages, in rendered views (a final sweep in every profile),
in drafting payloads and in every agent tool result. The details and the known limits of the
detectors (for example, a first name alone is not caught) are on [[PII-and-Sampling-Policy]].

## The guard helper is not a sandbox

`sg.guard.check_sql(sql, never_query=[...])` is a conservative deny-list over text: one
statement, `SELECT`/`WITH` only, no DML/DDL keyword, no SQL-from-text or file/URL-reading
function families, no file-as-table. It refuses SQL whose string and comment structure differs
between dialects. It does **not** stop expensive or side-effecting read-only functions, and an
unlisted vendor function passes. It is an extra layer, never the only one. See
[[Feedback-and-Guard]].

## The agent tools

All agent tools read the local store, except `check_fact`, which runs a FactLang claim on the
warehouse. It is off by default (`Config.agent_checks="deny"`); with `"allow"` it runs within the
per-period measurement budget and returns only allow-listed evidence fields.

## LLM providers

Drafting sends table metadata and (policy-filtered) top values to the provider you configure.
Use `OpenAICompatibleProvider` with a local server, or `none` mode, if metadata itself must not
leave your network. API keys are read by the provider SDKs from their usual environment
variables; SchemaGuru does not store them.

## Reporting a vulnerability

See [`SECURITY.md`](https://github.com/rupakc/schemaguru/blob/main/SECURITY.md) in the repository.

## See also

- [[PII-and-Sampling-Policy]]
- [[Feedback-and-Guard]]
- [[Store]]
- [[FAQ]]
