`orders.deleted` is TRUE or NULL — never FALSE. The filter an agent writes from the
name, `WHERE NOT deleted`, returns **0 rows**; the live rows are
**9,700**. The measured values sit next to the human's soft-delete rule:

```text
  live rows: (deleted IS NULL OR NOT deleted) (human, unchecked)
    deleted      BOOLEAN  values: TRUE (300) (measured 2026-11-24); null 97% (measured 2026-11-24); distinct 1 (measured 2026-11-24)
```
