---
name: self-review
description: Review a change against the SchemaGuru design-decision checklist (spec section 25).
---

# self-review

1. Run `git diff` for the change and read every touched module.
2. Check each rule below and cite file:line for any violation: (1) unknown is `None`, never 0 or 1.0; (2) methods travel with numbers; (3) exact by default, sampled numbers `ESTIMATED` and never `VERIFIED`, no `LIMIT` sampling; (4) forward match and reverse coverage are distinct; (5) thin joins labelled, broken joins refused; (6) hub guard refuses a shape, not a node; (7) no role from names alone.
3. (8) expectations are alarms, never auto-edited; (9) measured types stated next to human notes; (10) no writes on the query path; (11) judge relative to the previous snapshot; (12) byte-deterministic exports and views; (13) consequence-ordered rendering with a legend that says silence is not verification.
4. (14) one bad item costs one item, loaders never raise on data; (15) soft-delete flags may be TRUE/NULL, never drop a load-bearing column by name; (16) composite keys declared and grouped in full; (17) a narrower run never overwrites a fuller snapshot; (18) waivers expire; (19) timestamps tz-aware UTC; (20) no proprietary names.
5. Also check: layer rule, FactLang seam rule, no measurement SQL, tests written first with negative controls.
6. Report findings as a numbered list; fix them before committing.
