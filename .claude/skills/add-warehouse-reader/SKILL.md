---
name: add-warehouse-reader
description: Add a catalog reader for a new warehouse dialect.
---

# add-warehouse-reader

1. Create one module `src/schemaguru/catalog/readers/<dialect>.py` implementing `CatalogReader`; metadata SQL over system views only, bind parameters for every value, run via the `MetadataExecutor`.
2. Import the driver lazily inside functions; add the extra to `pyproject.toml` and, if the module cannot run without credentials, to the coverage omit list.
2b. Optionally implement `view_definitions(tables)` (batched like `columns`, metadata SQL only, run through the guard) and set `ReaderCapabilities.view_definitions=True`; the crawl then parses each view's definition into lineage (`docs/guide/lineage.md`). A reader without it reports `False` and is never asked.
3. Record a metadata fixture JSON (rows of each system view used) under `tests/fixtures/catalog/<dialect>/`.
4. Register the reader in `src/schemaguru/catalog/registry.py`.
5. Add the dialect to the reader conformance test parameters (recorded fixture; live container or `tests/live/` opt-in).
6. Write the Warehouse Guide page `docs/wiki/Warehouse-Guide-<dialect>.md` (permissions, cost, quirks).
7. Run the full gate; self-review with the `self-review` skill.
