---
name: add-detector
description: Add a PII or shape detector.
---

# add-detector

1. Add the detector in `src/schemaguru/pii/`; decide from values, shape and cardinality, never from a name alone.
2. Define its mask and prove masking is idempotent and deterministic (hypothesis test).
3. Register it in the detector registry and in the default policy; masked stays the default sample mode.
4. Add positive and negative controls, including the anonymised-looking low-cardinality enum trap.
5. Have the `pii-privacy-reviewer` agent review the diff.
6. Run the full gate and the `self-review` checklist.
