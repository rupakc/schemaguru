---
name: run-fixture-warehouse
description: Build and query the generated shop fixture warehouse with known traps.
---

# run-fixture-warehouse

1. Generate the fixture with the generator in `tests/fixtures/shop/` (DuckDB by default; Postgres via the integration job).
2. Confirm the known traps are present: soft-delete flag TRUE/NULL, STRING dates, enum case drift, composite-PK junction, thin-but-correct join, broken join, hub with two attribution edges, reverse-coverage asymmetric join, PII column, anonymised-looking enum, empty table, all-null column.
3. Run `pytest -m integration` for the end-to-end tests asserting each trap is discovered and rendered.
4. Review golden-view diffs by hand before accepting them.
5. Use only the generic shop domain.
