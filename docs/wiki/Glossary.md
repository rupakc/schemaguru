# Glossary

> Every term this wiki uses, in alphabetical order, with a one- or two-sentence definition.
> Terms that name code (classes, settings) are in `code` font.
> Follow the links for the full story.

**Attribution edge.** A join edge into a hub whose measured fan-in (child rows per key) exceeds
`attribution_fanin` (10.0): many child rows attributed to one parent. [[Join-Graph]]

**Block sampling (`system`).** Sampling whole storage blocks of a table; it actually reduces
bytes read, but rows are clustered, so intervals are widened and labelled approximate. [[Sampling]]

**Bridge table.** A table added to a view because it lies on the best join path between two seed
tables. [[Scoping]]

**Candidate (join candidate).** A join worth measuring (declared FK, overlay, dbt test, name
heuristic, LLM suggestion). It is not a fact until measured. [[Measurement]]

**Cardinality.** `N:1`, `1:1`, `1:N` or `N:M`, from the measured maximum rows per key on each side.

**Chain (identity chain).** A named, human-written join recipe; linear chains compile into
waypoint rules. [[Waypoint-Rules]]

**`ContextView`.** The object `guru.context()` returns: ordered blocks, the shown tables, a
fingerprint and a token count. [[Rendering]]

**Coverage.** The reverse direction of a join: the share of distinct parent keys that some child
row reaches. Different from match rate. [[Measurement]]

**Crawl.** Reading the warehouse's catalog metadata into the store; never data rows.
[[Catalog-and-Crawl]]

**Detail level.** How much of a table block is rendered: `full`, `standard`, `lite`, `name-only`.
[[Scoping]]

**Diagnostic.** A reported finding with a code (`SGnnn`), severity, message, location and hint,
used instead of an exception. [[Diagnostics]]

**Draft.** A fact written by an LLM (`DRAFTED`), shown with "(draft)" until verified or accepted.
[[Drafting]]

**Drift.** A difference between two snapshots of the warehouse's knowledge. A *pre-existing*
contradiction is drift, not a regression. [[Refresh-and-Drift]]

**Effective knowledge.** What the agent is told for one (kind, subject) after the precedence
rules have combined all facts. [[Concepts]]

**`ESTIMATED`.** The status of a number measured on a sample; never `VERIFIED`. [[Sampling]]

**Expectation.** A human or declared statement that is checked against the data and never
overwritten by it: values, joins, primary keys, soft-delete predicates. [[Overlays]]

**FactLang.** The claim language SchemaGuru depends on for every measurement, SQL rendering and
verdict ([github.com/rupakc/factlang](https://github.com/rupakc/factlang)). [[Architecture]]

**Fact.** The unit of knowledge: kind, subject, value, provenance, status, method, evidence,
source, time. [[Concepts]]

**Fan-in.** Child rows per distinct child key of a join.

**Fingerprint.** `sha256:` of a view's text; equal text, equal fingerprint. [[Rendering]]

**First touch.** A question scoping to a never-measured table queues it for measurement.
[[Scoping]]

**Hub.** A table joins point into but none leave, with an entity type or at least two attribution
edges. Fine as an endpoint; the guard refuses pivoting through it. [[Path-Search-and-Hub-Guard]]

**`HUB_PIVOT`.** The refusal for routes that arrive at a hub on one attribution edge and leave on
another: a cartesian hazard.

**Key-hash sampling (`keyhash`).** Keeping rows whose hashed key falls in a bucket range, with
the same hash on both sides of a join so the same keys are kept. [[Sampling]]

**Ledger.** The store's record of every byte, token and query spent. [[Store]]

**Lineage.** What a table or column is built from or feeds, from dbt, view definitions or overlays: `LINEAGE` (table) and `COLUMN_LINEAGE` (column) facts, always unverified and never a join path. [[Lineage]]

**Match rate.** The forward direction of a join: the share of distinct child keys found in the
parent. [[Measurement]]

**Method.** How a number was measured: `exact`, or `sampled:<method>:<fraction>:<seed>`.
Numbers with different methods are never compared.

**Never query.** An overlay entry telling agents not to use a table; best-effort, not access
control. [[Security]]

**Overlay.** A human-written YAML file of knowledge; its facts are `HUMAN` and always beat
drafts. [[Overlays]]

**Precedence.** The fixed rules that decide which fact wins. [[Concepts]]

**Profile.** A rendering for one role: `agent`, `planner`, `verifier`, `router`. [[Rendering]]

**Provenance.** Where a fact came from: `CATALOG`, `DECLARED`, `MEASURED`, `DRAFTED`, `HUMAN`.
[[Concepts]]

**Provisional path.** A route that uses a join thinner than `thin_support` (5%); offered last and
labelled. [[Path-Search-and-Hub-Guard]]

**Refusal.** A typed "no route" answer: `NO_EVIDENCE`, `SAME_TABLE`, `NO_ROUTE`,
`WAYPOINT_UNSATISFIABLE`, `RULE_REJECTS_ALL`, `HUB_PIVOT`.

**Schema intelligence.** Measured, labelled knowledge about a warehouse that an agent needs to
query it correctly. [[Why-Schema-Intelligence]]

**Seed table.** A table retrieval returns for a question. [[Scoping]]

**Snapshot.** A frozen copy of the facts at a refresh, with the checks that ran.
[[Refresh-and-Drift]]

**Soft delete.** Rows marked deleted but still present; the `SOFT_DELETE` fact holds the filter
that keeps live rows. Flags may be TRUE or NULL, never FALSE.

**Status.** How far a fact is checked: `OBSERVED`, `VERIFIED`, `CONTRADICTED`, `UNVERIFIED`,
`ESTIMATED`, `UNMEASURABLE`, `ERROR`, `STALE`, `WAIVED`. [[Concepts]]

**Store.** The local database of everything SchemaGuru knows (SQLite by default). [[Store]]

**Support.** The match rate (child → parent) or coverage (parent → child) of a hop.
[[Join-Graph]]

**Thin join.** A join with support under 5%: offered, labelled, not refused.

**Value policy.** `masked`, `raw` or `none`: what data values may leave SchemaGuru.
[[PII-and-Sampling-Policy]]

**Waiver.** A human acknowledgement of a contradiction, with a mandatory expiry date.
[[Overlays]]

**Waypoint rule.** A human constraint on join routes (require or forbid nodes, columns or edges).
[[Waypoint-Rules]]

## See also

- [[Concepts]]
- [[FAQ]]
- [[Home]]
