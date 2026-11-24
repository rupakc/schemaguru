# Refresh and Drift

> A refresh re-crawls incrementally, re-measures what is stale or changed, takes a snapshot and compares it with the previous one, fact by fact.
> Every change is classified: benign, needs review, a pre-existing contradiction, or not measured this time (which is never reported as "fixed").
> The result is a report in JSON or Markdown plus an exit code, so a CI job can fail when the warehouse drifted in a way a person should look at.

## Running a refresh

```python
report = guru.refresh(label="nightly")
print(report.exit_code, report.counts())
print(report.to_markdown())          # or report.to_json()
```

or `schemaguru refresh --url ... [--format markdown|json]`. The exit code is the report's.

## What a refresh does

1. **Incremental crawl** ([[Catalog-and-Crawl]]): new, changed and withdrawn tables recorded.
2. **Re-measure** live tables that were never measured, were altered since their newest
   measurement, or whose newest measurement is older than `fact_ttl_days` (14), within budget.
   Re-measuring a table re-runs every probe planned for it, which re-checks the human and declared
   expectations (values, keys, joins) on it.
3. **Snapshot** the facts, recording which checks actually ran (`checks_run`).
4. **Diff** the new snapshot against the previous one by fact identity and content, **never by
   timestamps** (a re-measurement that found the same number is not a change).
5. **Classify** every change.

## Classes

| Class | Meaning | Examples |
|---|---|---|
| `BENIGN` | expected movement | a measurement moved but kept its verdict; enum values only added; new non-PII columns or tables; PK still unique |
| `REVIEW` | a person should look | a verdict changed; facts removed; enum values removed or renamed; new PII-looking columns; withdrawn tables; an expectation newly contradicted |
| `PRE_EXISTING` | drift that was already there | a contradiction the previous snapshot already had: not a new regression |
| `NOT_MEASURED` | the check did not run this time | the probe errored, the table was not due, the budget ran out |

Anything a rule does not recognise is `REVIEW`. "New PII-looking columns" are flagged by name
*for routing to a person* only; nothing is ever recorded as PII because of a name.

**Judged relative to the previous snapshot.** Data moves between snapshots. A contradiction that
was already present is reported as drift, not as a regression of the new snapshot, so a nightly
job does not fail every night for the same known issue.

**A narrower run never overwrites a fuller one.** If this refresh measured fewer checks than the
last (an error, a budget cap), the missing checks are listed as `NOT_MEASURED`, never as
"removed" or "fixed".

## Exit codes

| Code | When |
|---|---|
| 0 | nothing needs review (also when everything was fresh and nothing had to be measured) |
| 1 | at least one change needs review |
| 3 | the crawl failed with nothing seen, a measurement error was reported, or measurement was attempted and nothing was measured |

## A worked example

The shop fixture was measured and a baseline refresh taken. Then, in the warehouse: 40 events
got a new `kind` value `refund`, the `customers.segment` value `vip` was renamed to `VIP`, and a new
table `shop.users (user_id, email)` was created. Fifteen days later (past the 14-day TTL):

```python
report = guru.refresh(label="two-weeks-later")
print(report.to_markdown())
```

Real output (SchemaGuru 0.1.0, 2026-11-24 with a simulated clock; stand-in measurement backend):

```markdown
# Refresh report

- exit code: 1
- tables seen 15, written 15, withdrawn 0
- measured 15 table(s), 127 fact(s), 4515360 bytes
- changes: review 3, not_measured 0, pre_existing 0, benign 15

## Needs review (3)

- DISTINCT shop.users.email: added
- NULL_RATE shop.users.email: added
- VALUES shop.customers.segment: value

## Benign (15)

- DISTINCT shop.events.event_id: value
- DISTINCT shop.events.kind: value
- DISTINCT shop.users.user_id: added
- JOIN shop.events.account_id -> shop.accounts.account_id: value
- JOIN shop.events.session_id -> shop.sessions.session_id: value
- NULL_RATE shop.events.account_id: value
- NULL_RATE shop.events.event_id: value
- NULL_RATE shop.events.kind: value
- NULL_RATE shop.events.session_id: value
- NULL_RATE shop.users.user_id: added
- PRIMARY_KEY shop.events: value
- PRIMARY_KEY shop.users: added
- ROW_COUNT shop.events: value
- ROW_COUNT shop.users: added
- VALUES shop.events.kind: value
```

How to read it:

- `VALUES shop.customers.segment: value` is **review**: a value disappeared (`vip`) and another
  appeared. A rename can break every query that filters on the old value.
- The new `users.email` column's facts are **review**: the column name looks personal, so a
  person should decide its value policy. (`users.user_id` is benign.)
- `VALUES shop.events.kind: value` is **benign**: values were only added.
- The join and count movements on `events` kept their verdicts: **benign**.

Exit code 1 makes a CI job fail until someone has looked.

## Using it in CI

```bash
schemaguru refresh --url "$SCHEMAGURU_URL" --format markdown > refresh-report.md
```

A non-zero exit fails the job; attach the Markdown report as an artifact or post it to a pull
request. Pair it with view fingerprints ([[Rendering]]) to see whether the agent's context
changed, and with `schemaguru lint` to keep overlays valid.

## Custom classification

A `ChangeClassifier` can override the default rules for changes it has an opinion about (for
example "a NULL_RATE move under 1% is benign for this schema"). See
[[Extending-ChangeClassifier]].

## See also

- [[Measurement]] - what re-measuring runs
- [[Store]] - snapshots
- [[CLI]] - `schemaguru refresh` and exit codes
- [[Design-Decisions]] - relative judgement and why missing checks are not fixes
