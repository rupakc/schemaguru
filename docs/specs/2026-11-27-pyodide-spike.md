# Pyodide spike: the real wheels on the playground store (2026-11-27)

Question: can the docs playground run the released SchemaGuru and FactLang wheels in the
browser, offline, over a store built at docs-build time? Answer: yes, every core call works.

## Setup

- Pyodide 0.29.0 (npm package, run under Node 20.18.1), Python 3.13.2, SQLite 3.39.0.
  `loadPackage(["sqlite3", "pyyaml"])` fetched both from the Pyodide CDN.
- Wheels: `schemaguru-0.2.0-py3-none-any.whl` (built from this tree) and
  `factlang-0.1.4-py3-none-any.whl` (PyPI), both loaded with `pyodide.unpackArchive`.
  sqlglot and duckdb are absent, as in the browser.
- Store: `scripts/build_playground_store.py` (the shop fixture plus three `analytics` views,
  crawled with view lineage, fully measured, spec overlay loaded; journal_mode DELETE, VACUUMed).
- Harness: `node scripts/pyodide_check.mjs <pyodide pkg dir> build/playground`, after PREP
  (see the redesign plan). The glue is `site-playground/glue.py`, imported as a module.

## Results

| Capability | Result | Evidence |
|---|---|---|
| Import of both wheels | PASS | `import glue` (imports `schemaguru`, which imports `factlang`): schemaguru 0.2.0 |
| sqlite3 + store open | PASS | `sg_open` ok, 17 tables. The store opens read-write, so SchemaGuru sets WAL (`src/schemaguru/store/sqlite.py:109`); `PRAGMA journal_mode` after open = `wal` on Pyodide's in-memory FS, and later writes (overlay load, measurement queue) succeed |
| FTS5 | PASS | `CREATE VIRTUAL TABLE temp.fts5_probe USING fts5(x)` succeeds; the store's recorded search backend after open is `fts5` |
| `context` | PASS | "monthly revenue by customer segment": 1702 tokens, 5 tables; same token count as CPython |
| `find_join_path` | PASS | shop.orders → shop.events refused `HUB_PIVOT`; shop.payments → shop.customers: no refusal, 5 paths |
| SQL join snippet | PASS | the context view for "payments per customer" carries 6 `SQL: FROM ... JOIN ...` lines and no `SG501`: FactLang renders the duckdb join without sqlglot (no FL504). The `find_join_path` tool itself never renders SQL (it lists join keys, in CPython too); the snippet comes from the context view's join-path section |
| `lineage` | PASS | analytics.revenue_by_segment upstream, depth 3: revenue_by_segment, paid_revenue, shop.customers, paid_orders, shop.payments, shop.orders |
| `mask_free_text` | PASS | "write to jane.doe@example.org" → "write to \<email\>", 1 hit |
| `load_overlays` from an FS path | PASS | `sg_open` loads `/playground/base_overlay.yaml`; context views show "human overlay" provenance |
| Clean CPython venv (wheels + PyYAML only) | PASS | `python -I glue.py build/playground` → `ok 0.2.0 fts5` (`test_glue_in_clean_venv`) |

Sizes (bytes): schemaguru wheel 456,823; factlang wheel 215,058; `playground.db` 421,888.

Wall times under Node on the build machine (one run; a browser will differ): loadPyodide
911 ms, loadPackage 89 ms (cached), unpack wheels 26 ms, `import glue` 724 ms, smoke 124 ms,
`sg_open` 19 ms, `sg_context` 52-58 ms, `sg_path` 1-3 ms, `sg_lineage` 2 ms, `sg_mask` < 1 ms.

## Findings

- The FTS5 probe the plan named, `CREATE VIRTUAL TABLE temp.x USING fts5(x)`, fails on every
  SQLite (also CPython's 3.45.1: "vtable constructor failed: x") because FTS5 rejects a column
  named like its table. The harness probes `temp.fts5_probe` instead, as the library does.
- The `analytics` views share `order_id`/`customer_id` with the shop tables, so measurement
  finds joins through them: join paths and context views now list routes such as
  shop.customers → analytics.paid_revenue (support 22.6%). That is real output, not an error,
  but the Join-path tab will show views as hops.
- Node's `Buffer` must be wrapped in a `Uint8Array` for `unpackArchive` (a browser's
  `ArrayBuffer` from `fetch` is fine).

## Decisions

- FTS5 is present: the Ask tab needs no LIKE-fallback note on Pyodide 0.29.0. It keeps the
  rule of Builder-check 6: if `sg_open` reports `search_backend` other than `fts5`, the tab shows
  "search ranking uses the LIKE fallback in this browser".
- No call FAILed, so no tab is precomputed. Should one fail in a later browser, Task 2
  precomputes that tab's outputs into the manifest under `precomputed.<tab>` and the tab shows
  them with a visible "precomputed at build time: \<reason\>" note.
- The SQL snippet renders in Pyodide. If it ever cannot (for example FL504 without sqlglot), the
  playground shows the path without SQL plus a visible note; it never shows a made-up snippet.
- The wheels import and the store opens, so the STOP case (no live playground) does not apply.
