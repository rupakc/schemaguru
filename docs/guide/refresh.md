# Refresh and drift

Warehouses move. `guru.refresh()` (or `schemaguru refresh`) brings the knowledge up to date
and tells you what changed — judged against the previous snapshot, not in absolute terms.

1. **Incremental crawl**: new, changed and withdrawn tables ([Crawl](crawl.md)).
2. **Re-measure** facts older than `fact_ttl_days` (14) and every fact on a changed table,
   within budget, highest priority first.
3. **Re-check** the human and declared expectations on the touched tables.
4. **Snapshot and diff**: facts are compared by identity and value, never by timestamp.
5. **Classify** each change:

| Class | Examples |
|---|---|
| `BENIGN` | a measurement moved but kept its verdict; enum values only added; a new non-PII column or table; a key still unique |
| `REVIEW` | a verdict changed; a fact was removed; enum values removed or renamed; a new PII-looking column; a withdrawn table; an expectation newly contradicted; anything unrecognised |
| `PRE_EXISTING` | a contradiction the previous snapshot already had: drift, not a regression of this run |
| `NOT_MEASURED` | a check that did not run this time (budget, error, table not due): never reported as "removed" or "fixed" |

A refresh that measured less than the previous one never silently overwrites it: the new
snapshot records the checks it ran, and what it could not check is listed as missing.

```python
import schemaguru as sg

guru = sg.Guru.connect("duckdb:///warehouse.duckdb")
first = guru.refresh(label="baseline")
second = guru.refresh(label="nightly")
print(second.exit_code, second.counts())
print(second.to_markdown()[:400])
guru.close()
```

## Exit codes for CI

| Code | Meaning |
|---|---|
| `0` | nothing needs review |
| `1` | at least one `REVIEW` change |
| `3` | errors, or nothing could be measured |

`report.to_json()` and `report.to_markdown()` give the full report — post the Markdown on a
pull request or a chat channel. A typical nightly job:

```bash
schemaguru --url "$WAREHOUSE_URL" refresh --label nightly --format markdown > refresh.md
```

Pair it with [view fingerprints](rendering.md#fingerprints-for-ci) to see whether the prompts
your agents read changed, and with [overlays](overlays.md) to turn a recurring finding into a
declared expectation (or a waiver with an expiry).

New PII-looking columns are routed to review by name (`email`, `phone`, `iban`, …) — a
conservative routing to a person, never a classification: nothing is recorded as PII
because of a name. Custom rules: [Change classifier](../extending/change-classifier.md).
