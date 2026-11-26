# Overlays (human YAML)

An overlay is where people write what the data cannot say: what a table means, which values
a column *should* take, how to find live rows, which joins to trust, what a business term
means. Overlays are plain YAML — reviewable in pull requests, diffable, owned by people.
**SchemaGuru never edits a file you wrote.**

## The format

One `schemaguru.yaml`, or a directory `schemaguru/*.yaml`. This is the complete example the
test suite loads (spec §14.1):

```yaml
--8<-- "tests/fixtures/overlays/spec_example.yaml"
```

| Section | Holds | Becomes |
|---|---|---|
| `tables.<t>.description`, `use_cases`, `entity_type`, `pitfalls` | meaning | `HUMAN` facts that always beat drafts |
| `tables.<t>.columns.<c>.values` (+ `values_split`) | an expected enum | an **expectation**: checked, never edited; contradictions shown |
| `tables.<t>.soft_delete` | a FactLang filter giving the *live* rows | a `SOFT_DELETE` expectation |
| `tables.<t>.columns.<c>.type_note` | how a column is really stored | a note shown next to the measured type |
| `tables.<t>.joins` | a join you vouch for (`to`, `from`, `cardinality`, `cast_to`) | a `JOIN` expectation (a candidate to measure) |
| `chains` | a join recipe, `steps` of joins, filters and notes, with `because` | compiled into a waypoint rule |
| `waypoints` | route rules for path search ([graph](graph.md#waypoint-rules)) | rules |
| `glossary` | `term`, `definition`, `maps_to` tables | glossary facts; their tables join the scope when the term is asked about |
| `never_query` | tables agents must not use, with a `reason` | listed in every view; `sg.guard.check_sql(..., never_query=...)` refuses SQL reading them |
| `waivers` | an acknowledged contradiction, with `reason` and a mandatory `expires` | `WAIVED` until the date |
| `lineage` | where a table or column comes from: `table` + `upstream`, or `column` + `inputs` + `transform`; `ignore: true` hides parsed links | `LINEAGE` / `COLUMN_LINEAGE` facts, human-written; see [Lineage entries](#lineage-entries) |

## Loading

```python
import schemaguru as sg

guru = sg.Guru.connect("duckdb:///warehouse.duckdb")
guru.measure("shop.orders")
for d in guru.load_overlays("schemaguru.yaml"):
    print(d.code, d.severity.value, d.location, d.message)
print(guru.context("refund rate by month").text)
guru.close()
```

* **It never raises on data.** Every problem is a diagnostic with `file:line`: unsupported
  `version` (`SG201`), an unknown key with a close-match suggestion (`SG202`), a reference to
  nothing in the store (`SG203`) or to several things (`SG204`, candidates listed), an invalid
  filter (`SG205`), a waiver without a valid `expires` (`SG206`) or already expired
  (`SG207`), a malformed entry (`SG208`). An empty key counts as absent.
* **One call, all files.** Each `load_overlays` call *replaces* the previously loaded rules,
  chains, waivers and overlay facts. Pass every overlay path in one call:
  `guru.load_overlays("schemaguru/", "team-a.yaml")`. An entry you delete from a file (or a
  file you no longer pass) has its facts retracted from the store, and so does an entry with
  an error of its own (an `SG208` on that entry retracts that entry's previous fact; the rest
  of the file loads). A file that fails as a whole (unreadable, not YAML, not a mapping,
  unsupported `version`) keeps its previous facts until it loads again.
* **What persists.** The `HUMAN` facts (descriptions, expectations, glossary, …) are written
  to the store. Waypoint rules, chains and waivers live on the `Guru` that loaded them; load
  the overlays in the process that serves the agent.
* **Filters** (soft delete, chain filters) are checked with FactLang's parser; with a FactLang
  that lacks it, syntax only (`SG503`, informational). In SQL snippets SchemaGuru quotes a
  soft-delete column in the catalog's exact case; when two columns differ only in case, the
  exact spelling wins, and a name matching several of them stays unquoted (`SG506`).
* **A soft-delete filter is checked against the data.** It must name existing columns and,
  when it compares one column with values, at least one of them must occur in that column's
  measured values (`TRUE`/`FALSE` need a boolean column; NULLs are always fine). A mismatch is
  `SG210`: the view warns and SQL snippets leave the filter out. The text is never edited.

Lint overlays in CI without touching your store — it loads them into a throwaway copy and
exits `1` on findings:

```bash
schemaguru lint schemaguru.yaml schemaguru/
```

!!! note "The CLI and overlays in 0.1"
    In 0.1 only `schemaguru lint` reads overlay files. The other commands (`context`, `path`,
    `mcp`, …) serve what is in the store, so load overlays from Python
    (`guru.load_overlays(...)`) to put their facts there.

## Expectations are alarms

A declared enum, join, key or soft-delete rule is never "corrected" to match the data. When
measurement disagrees, both are shown and the disagreement is a warning:

--8<-- "generated/story_case_drift.md"

Acknowledge a known contradiction with a waiver; it expires, and an expired waiver is
ignored and reported.

## Drafts and accepted drafts

LLM drafts never go into your files. `schemaguru export-drafts schemaguru/drafts.yaml`
writes them to a separate, generated file for review; `schemaguru accept <ids>` (or
`review`) stores accepted drafts as `HUMAN` facts and, with `--write-overlay`, appends them
to a machine-owned `schemaguru/accepted.yaml`.

## dbt

`schemaguru import-dbt target/manifest.json` (or `sg.overlays.import_dbt(path, guru.store)`)
maps model and column descriptions to `DECLARED` descriptions, `relationships` tests to
joins, `accepted_values` to expected values, and a single `unique` + `not_null` column to a
primary key — all `DECLARED`, `UNVERIFIED` until measured. Only models and columns present in
the store are mapped; an unreadable manifest is `SG209`.

## Validation

Loading never raises on data; every problem is a line-numbered diagnostic and the entry is
skipped. A key written twice in one mapping is `SG208` naming both lines, and neither entry is
applied. A waiver's `fact` must start with a registered fact kind (`SG208` otherwise) and its
subject glob should match something in the store (`SG203`, a warning: the waiver still loads
but can never apply). Glossary `maps_to` takes tables only; a column is `SG203`.

## Known limits

- A malformed overlay entry's previous facts are retracted on the next load: fix the entry and
  reload. (A file that fails as a whole, unreadable or not YAML, keeps its previous facts.)
- Join entries in an overlay are declarations. They carry a `cardinality` you vouch for and
  nothing else; measured numbers (match rate, coverage) come only from measurement, never from
  a human or a dbt file.
- `export-drafts` and `accept --write-overlay` write only kinds that have an overlay form;
  the others are left out and named in a warning (and printed by `export-drafts`).

## Lineage entries

Where lineage comes from, what agents see and its limits: [Lineage](lineage.md).

```yaml
lineage:
  - table: analytics.revenue_daily
    upstream: [shop.orders, shop.refunds]
  - column: analytics.revenue_daily.net_revenue
    inputs: [shop.orders.amount, shop.refunds.amount]
    transform: aggregate        # copy | cast | expression | aggregate | window | unknown (default)
  - table: analytics.legacy_view
    upstream: [shop.orders]
    ignore: true                # hides this parsed link; human always wins
```

Each entry is one human fact (`source` `overlay:<file>:<line>`). Exactly one of `table` / `column`;
`upstream` goes with `table`, `inputs` with `column`; every name must resolve (`SG203` / `SG204`
drop the entry); a non-ignore entry needs a non-empty list; a malformed entry is `SG208`.

- A human entry with a list **replaces** every parsed link (view definition, dbt) of that subject and
  level. Table and column levels are independent.
- `ignore: true` **without** a list hides all parsed links of that level for the subject; **with** a
  list it hides only a parsed link whose sorted list is exactly equal. Hidden facts stay in the
  store (`explain` shows them) and return when the entry is removed.
- An ignore that hides nothing is `SG213` (a warning, reported by `load_overlays` and `schemaguru
  lint`): typically the view was redefined after the ignore was written, so its old link is gone and
  the new one is shown. Update the ignore to the new list or drop it.
- Two identical lines are one fact (kept at the first line). Two lines with the same column inputs but
  a different `transform` are `SG208` and neither is applied; keep one.
