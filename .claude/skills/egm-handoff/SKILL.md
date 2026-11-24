---
name: egm-handoff
description: Cut a plan task into a self-contained brief for a goldfish implementer (Elephant-Goldfish method).
---

# egm-handoff

1. Pick one task from the plan. It must be implementable from the spec plus the task text plus the repo; if not, amend the spec first and record it in `MEMORY.md`.
2. Write the brief: the task text verbatim, the exact files, the interfaces it produces and consumes, and the global constraints from `CLAUDE.md`.
3. Name only the spec sections that matter; do not hand over the whole plan or prior chat.
4. State the done criteria: tests first with negative controls, full gate green, explicit-path commit, pushed, CI green.
5. Send it to a fresh `goldfish-implementer`; same-layer tasks run sequentially, independent tasks may run in parallel.
6. When it reports, send the diff and the spec to a separate fresh reviewer (`goldfish-spec-reviewer`, plus `graph-algorithms-reviewer` or `pii-privacy-reviewer` where relevant).
7. Any gap the goldfish reports is a spec defect: amend the spec, add a `MEMORY.md` entry, re-issue the task.
