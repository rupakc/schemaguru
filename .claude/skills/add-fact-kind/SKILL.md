---
name: add-fact-kind
description: Add a new kind of fact to the knowledge model.
---

# add-fact-kind

1. Add the `FactKind` member and its value schema in `src/schemaguru/model/`; state its method and whether it can be `ESTIMATED`.
2. Unknown is `None`: define what the absent value means and never default it.
3. Add precedence behaviour in `knowledge/resolve.py` (overlay over measured over drafted) with an order-independence property test.
4. Add the probe in `measure/` as a FactLang claim or primitive; never write SQL.
5. Add rendering with consequence order and the legend text in `render/`; keep output byte-deterministic.
6. Update the fixture warehouse trap, golden views and `docs/wiki/Concepts.md`.
7. Run the full gate and the `self-review` checklist.
