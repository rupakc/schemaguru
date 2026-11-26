# Docs Site Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the SchemaGuru mkdocs-material site into a landing page, a Pyodide playground that runs the real wheel on a CI-built shop store, and a Features page, with every number on them produced by the library.

**Architecture:** A build script makes a small knowledge store (the shop fixture plus three DuckDB `analytics` views, crawled with lineage, measured, overlaid). One Python module, `site-playground/glue.py`, is the only thing the browser calls. It runs unchanged in CPython (tests, docs.yml smoke) and in Pyodide (the playground). The landing template reads `docs/generated/landing.json`, which `scripts/gen_docs.py` writes from real output, through an mkdocs hook. Every claim on the page is therefore generated and checked for currency like the existing generated docs.

**Tech Stack:** mkdocs-material (custom_dir overrides), Jinja, vanilla JS/CSS, Pyodide 0.29.0 (the version FactLang pins: `~/factlang/docs/playground.md:15`), DuckDB + sqlglot at build time only, pytest, Node 20 (spike harness), Playwright (local headless smoke, optional).

**Spec:** `docs/specs/2026-11-27-docs-site-redesign-design.md` (binding, including "Builder-check answers").

## Global Constraints

- Gate after every code task, chained with `&&`, never piped: `ruff check && ruff format --check && mypy --strict src && pytest -m "not integration and not live and not benchmark"`, plus `mkdocs build --strict` (with `NO_MKDOCS_2_WARNING=1`).
- From Task 6 on, `mkdocs build --strict` needs the playground assets. Build them first with **PREP**: `rm -rf build/playground && python3 -m build --wheel --outdir build/playground && python3 -m pip download "factlang==$(python3 -c 'import importlib.metadata as m; print(m.version("factlang"))')" --no-deps --only-binary=:all: -d build/playground && python3 scripts/build_playground_store.py --out build/playground`. `build/` is gitignored, so the store is never committed.
- Commit author `Rupak Chakraborty <rupakc@users.noreply.github.com>` (repo-local config already set). Each message ends with `Co-Authored-By: Claude <model> <noreply@anthropic.com>`. Use conventional commits, and every commit leaves the gate green.
- Stage explicit paths only. Never `git add -A` or `git add .`, never `--no-verify`, never override `core.hooksPath`, never `git stash`.
- Implementer and reviewer agents never dispatch subagents and never use `gh`.
- Never mention any proprietary predecessor or organisation (the CI `denylist` job, `tests/architecture/test_denylist.py`). Use the generic shop domain only.
- No third-party logo artwork. Warehouses appear as text badges only.
- No invented numbers. Every figure on a page comes from `docs/generated/*` (written by `scripts/gen_docs.py`), from `benchmarks/results/0.2.0.json` via a generator, or from live playground output. The trust strip carries no figures (Builder-check 5).
- Library behaviour stays unchanged. Any helper added under `src/` gets unit tests and passes `mypy --strict`.
- Red first for code: write the failing test, watch it fail for the stated reason, then implement.
- Python 3.10+ syntax (`target-version = "py310"`), line length 100. `scripts/` and `site-playground/` are linted by ruff; `src/` is also type-checked.
- `mkdocs.yml` sets `theme.font: false`. The only network calls the playground page may make are to the Pyodide CDN (`https://cdn.jsdelivr.net/pyodide/v0.29.0/full/`) and to the site's own files (D2).
- Privacy examples use obviously synthetic values only. No string may match a real provider-key format (Builder-check 4).
- Every page works at 360 px width without horizontal scroll, in light and dark scheme, with AA contrast, keyboard access and `prefers-reduced-motion` respected (D4).
- Use `python3`, and the venv at `~/schemaguru/.venv`.

## Review Focus

1. **The visitor cannot run the playground** (no WebAssembly, CDN blocked, offline). Expected: within seconds, a clear message linking Getting started and the guides, never an endless spinner. Pinned by `test_playground_without_webassembly_explains` in Task 6.
2. **Bad input in a tab** (an empty question, an unknown table in Join path or Lineage, malformed YAML in Override, a huge paste). Expected: a typed error envelope rendered as text, and the playground stays usable. Pinned by `test_glue_errors_are_envelopes` in Task 2.
3. **Override, then Reset.** Expected: the human edit wins in the view, and Reset restores the original view exactly (same fingerprint) without a reload. Pinned by `test_override_wins_and_reset_restores` in Task 2.
4. **Hostile or HTML text pasted into Privacy or Override** (`<img src=x onerror=…>`). Expected: shown as literal text, no element is created, and masking still applies. Pinned by `test_pasted_html_is_inert` in Task 6.
5. **Narrow screen, dark scheme, reduced motion.** Expected: no horizontal scroll at 360 px, content visible without waiting for animation, dark tokens applied. Pinned by `test_landing_narrow_dark_reduced_motion` in Task 5.

---

## File map

| Path | Responsibility | Task |
|---|---|---|
| `scripts/_shop_docs.py` (modify) | `shop_guru(..., views=())` hook to add DuckDB views before the crawl | 1 |
| `scripts/build_playground_store.py` (create) | playground fixture → `playground.db`, `playground.json`, `base_overlay.yaml` | 1, 2 |
| `site-playground/glue.py` (create) | the only Python the browser calls; JSON strings in and out | 1, 2 |
| `scripts/pyodide_check.mjs` (create) | Node harness: runs `glue.smoke` inside real Pyodide 0.29.0 | 1 |
| `docs/specs/2026-11-27-pyodide-spike.md` (create) | spike outcome record (excluded from the site) | 1 |
| `tests/conformance/test_playground.py` (create) | store shape, glue in CPython, clean-venv glue (integration) | 1, 2 |
| `docs/assets/schemaguru.css`, `docs/assets/schemaguru.js`, `docs/assets/favicon.svg` (create); `docs/assets/extra.css` (delete) | visual system | 3 |
| `docs/overrides/home.html` (create) | landing template | 5 |
| `scripts/gen_docs.py` (modify) | `landing.json`, `scale_claim.md` | 4 |
| `scripts/mkdocs_hooks.py` (modify) | `on_env` landing data; `on_files` playground assets; demo removal | 4, 6, 8 |
| `docs/index.md` (rewrite) | landing body: integration tabs + install | 5 |
| `docs/playground.md`, `site-playground/playground.{js,css}` (create) | playground page | 6 |
| `docs/features.md` (create) | features + inline SVG architecture | 7 |
| `tests/conformance/test_site_theme.py`, `tests/conformance/test_landing.py`, `tests/conformance/test_mkdocs_hooks.py`, `tests/site/test_site_smoke.py` (create) | theme, landing, hook, headless checks | 3–7 |
| `.github/workflows/docs.yml`, `mkdocs.yml`, `README.md`, `CHANGELOG.md` (modify); `docs/demo.md`, `site-demo/`, `scripts/build_demo_export.py` (delete) | retire the demo, CI | 8 |

---

### Task 1: Spike — the real wheels run in Pyodide on the playground fixture

**Files:**
- Modify: `scripts/_shop_docs.py:83-121` (`shop_guru`)
- Create: `scripts/build_playground_store.py`, `site-playground/glue.py` (core entry points), `scripts/pyodide_check.mjs`, `docs/specs/2026-11-27-pyodide-spike.md`
- Test: `tests/conformance/test_playground.py`

**Interfaces:**
- Consumes: `_shop_docs.build_db`, `QUESTIONS`, `NOW`, `OVERLAY`; `tests.unit.measure.fake_backend.FakeBackend`; `schemaguru.Guru(store=..., connector=None, dialect=..., name=..., now=...)` (`src/schemaguru/guru.py:196`); `Guru.context/find_path/lineage/handle_tool_call/load_overlays`; `schemaguru.pii.mask.mask_free_text` (`src/schemaguru/pii/mask.py:65`).
- Produces:
  - `shop_guru(directory: Path, *, views: Sequence[str] = ()) -> Iterator[ShopGuru]`. Each SQL string in `views` runs on a writable DuckDB connection after `build_db` and before the crawl. The default `()` leaves every generated doc byte-identical.
  - `scripts/build_playground_store.py`: `ANALYTICS_VIEWS: tuple[str, ...]`, `build(out: Path) -> dict[str, Any]` (returns the manifest), `main(argv: list[str] | None = None) -> int` (`--out`, default `ROOT / "build" / "playground"`).
  - Files in `out`: `playground.db` (journal_mode DELETE, VACUUMed), `base_overlay.yaml` (a copy of `OVERLAY`), and `playground.json` = `{"format": "schemaguru-playground/1", "schemaguru_version": str, "now": ISO-8601 str, "store": "playground.db", "overlay": "base_overlay.yaml", "tables": [canonical str, sorted], "lineage_subjects": [str], "questions": [str], "search_backend_at_build": "fts5" | "like"}`.
  - `glue.py` (each function returns a JSON **string**; failures return `{"error": {"code": str, "message": str}, "text": "<code> <message>"}`, the tool layer's shape, `src/schemaguru/agent/dispatch.py:90`):
    - `sg_open(store_path: str, manifest_json: str) -> str` → `{"ok": true, "schemaguru": str, "search_backend": str, "tables": [str]}`. Opens a module-level `Guru(store=store_path, connector=None, dialect="duckdb", name="shop", now=<manifest now>)` and loads the base overlay that sits next to the store.
    - `sg_context(question: str, profile: str = "agent", budget_tokens: int = 4000) -> str` → `{"text", "tokens", "tables", "fingerprint", "profile"}`
    - `sg_path(a: str, b: str) -> str` → the `find_join_path` tool result
    - `sg_lineage(ref: str, direction: str = "upstream", depth: int = 2) -> str` → the `lineage` tool result
    - `sg_mask(text: str, mode: str = "masked") -> str` → `{"mode", "text", "hits"}`
    - `smoke(playground_dir: str) -> str` → `"ok <version> <search_backend>"`; raises on any surprise. `python glue.py <dir>` prints it.

The fixture's views use the real shop columns. `orders` has no amount; `payments.amount` does. Use exactly these:

```sql
CREATE SCHEMA IF NOT EXISTS analytics;
CREATE VIEW analytics.paid_orders AS
  SELECT order_id, customer_id, created_at FROM shop.orders
  WHERE lower(status) = 'paid' AND (deleted IS NULL OR NOT deleted);
CREATE VIEW analytics.paid_revenue AS
  SELECT p.order_id, po.customer_id, p.amount
  FROM shop.payments p JOIN analytics.paid_orders po ON p.order_id = po.order_id;
CREATE VIEW analytics.revenue_by_segment AS
  SELECT c.segment, SUM(r.amount) AS revenue, COUNT(DISTINCT r.order_id) AS orders
  FROM analytics.paid_revenue r JOIN shop.customers c ON r.customer_id = c.customer_id
  GROUP BY c.segment;
```

- [ ] **Step 1: Write the failing tests** in `tests/conformance/test_playground.py`. Use a module-scoped fixture `playground(tmp_path_factory)` that calls `build(out)` once (`pytest.importorskip("duckdb")`, `pytest.importorskip("sqlglot")`). Use a fixture `glue` that `exec`s `site-playground/glue.py` into a namespace, as FactLang does (`~/factlang/tests/conformance/test_docs_examples.py:164`).

```python
def test_store_has_shop_and_analytics(playground):
    m = json.loads((playground / "playground.json").read_text())
    assert m["format"] == "schemaguru-playground/1" and m["schemaguru_version"] == sg.__version__
    assert {"shop.orders", "shop.customers", "shop.payments", "shop.events",
            "analytics.paid_orders", "analytics.paid_revenue",
            "analytics.revenue_by_segment"} <= set(m["tables"])
    assert (playground / "playground.db").stat().st_size < 20 * 2**20
    assert (playground / "base_overlay.yaml").read_bytes() == OVERLAY.read_bytes()

def test_lineage_is_precomputed(playground):
    with sg.Guru(store=str(playground / "playground.db"), connector=None) as g:
        up = {n.ref for n in g.lineage("analytics.revenue_by_segment", depth=3).nodes}
        down = {n.ref for n in g.lineage("shop.orders", "downstream", 1).nodes}
    assert {"shop.payments", "shop.orders", "shop.customers"} <= up
    assert "analytics.paid_orders" in down

def test_glue_core_in_cpython(playground, glue):
    assert json.loads(glue["sg_open"](str(playground / "playground.db"),
                      (playground / "playground.json").read_text()))["ok"] is True
    view = json.loads(glue["sg_context"]("monthly revenue by customer segment"))
    assert view["tokens"] > 0 and "shop.orders" in view["tables"]
    assert json.loads(glue["sg_path"]("shop.orders", "shop.events"))["refusal"] == "HUB_PIVOT"
    ok = json.loads(glue["sg_path"]("shop.payments", "shop.customers"))
    assert ok["refusal"] is None and ok["paths"] and "JOIN" in ok["text"].upper()
    assert json.loads(glue["sg_path"]("shop.broken_child", "shop.ref_parent"))["refusal"] is not None
    assert "shop.payments" in json.loads(
        glue["sg_lineage"]("analytics.revenue_by_segment", "upstream", 3))["text"]
    masked = json.loads(glue["sg_mask"]("write to jane.doe@example.org", "masked"))
    assert "jane.doe@example.org" not in masked["text"] and masked["hits"] >= 1
    assert glue["smoke"](str(playground)).startswith("ok ")

@pytest.mark.integration
def test_glue_in_clean_venv(tmp_path):
    """The glue imports only the two wheels + PyYAML (no sqlglot, no duckdb): docs.yml's smoke."""
    # skip unless build/playground holds both wheels and the store (run PREP);
    # venv in tmp_path; pip install build/playground/*.whl; run `python -I glue.py build/playground`
    # from cwd=tmp_path; assert stdout starts with "ok "
```

Adjust the attribute names on lineage nodes (`.ref`) to `LineageResult` as it is in `src/schemaguru/knowledge/lineage.py`. Do not change the library. If the parser's real upstream set differs, the test asserts what the real parser yields, and the note records it.

- [ ] **Step 2: Run them and confirm they fail.** Run `pytest tests/conformance/test_playground.py -v`. Expected: FAIL with `ModuleNotFoundError`/`FileNotFoundError` (no builder, no glue).
- [ ] **Step 3: Add `views=` to `shop_guru`** (`scripts/_shop_docs.py`). Run the views on a writable `duckdb.connect(path)` that is closed before the read-only connection opens. Then run `pytest tests/conformance/test_docs_generated.py -q`. Expected: PASS (generated docs unchanged).
- [ ] **Step 4: Implement `build(out)`.** Call `shop_guru(tmp, views=ANALYTICS_VIEWS)`. Exit 2 with a message when `schemaguru.lineage.parse.lineage_available()` is false. Copy the measured store after `guru.close()`, then `PRAGMA journal_mode=DELETE; VACUUM`. `lineage_subjects` = the three views plus the `revenue` and `segment` columns of `analytics.revenue_by_segment`. `questions` = `QUESTIONS` + `("revenue by customer segment from the analytics views",)`. `search_backend_at_build` comes from the store's `crawl_state` search key (`src/schemaguru/store/sqlite.py:45`).
- [ ] **Step 5: Implement the core glue functions** listed in Interfaces. `sg_mask(..., "masked")` = `mask_free_text`. `sg_path`/`sg_lineage` go through `guru.handle_tool_call`. `sg_context` uses `guru.context(...)`. Catch `Exception` at each entry point and convert it to the envelope (code = the `SGnnn` prefix when present, else the exception class name).
- [ ] **Step 6: Run the tests and confirm they pass.** Run `pytest tests/conformance/test_playground.py -v -m "not integration"`. Expected: PASS.
- [ ] **Step 7: Run PREP, then the clean-venv test.** Run `pytest tests/conformance/test_playground.py::test_glue_in_clean_venv -v -m integration`. Expected: PASS. A failure here (e.g. an import of an extra at module level) is a spike finding: record it. Do not change the library.
- [ ] **Step 8: Write `scripts/pyodide_check.mjs`.** Usage: `node scripts/pyodide_check.mjs <pyodide_pkg_dir> <playground_dir>`. It dynamically imports `loadPyodide` from `<pyodide_pkg_dir>/pyodide.mjs` and calls `loadPackage(["sqlite3", "pyyaml"])`. It `unpackArchive`s both wheels and writes the store, manifest and overlay into the Pyodide FS. It runs `glue.py` and prints `smoke(dir)`, `sqlite3.sqlite_version`, whether `CREATE VIRTUAL TABLE temp.x USING fts5(x)` works, `PRAGMA journal_mode` after open, and the wall time of each call. Exit non-zero on any exception.
- [ ] **Step 9: Run the spike.** Run `npm install --prefix "$SCRATCH/pyodide" pyodide@0.29.0 && node scripts/pyodide_check.mjs "$SCRATCH/pyodide/node_modules/pyodide" build/playground`. Expected: `ok <version> <backend>`. Also record the byte sizes of both wheels and the store.
- [ ] **Step 10: Write the note** `docs/specs/2026-11-27-pyodide-spike.md`: date, Pyodide 0.29.0 / Python version, and a table with PASS/FAIL plus evidence for: import of both wheels; sqlite3 + store open (WAL: `src/schemaguru/store/sqlite.py:109`); FTS5; `context`; `find_join_path` incl. SQL snippet; `lineage`; `mask_free_text`; `load_overlays` from an FS path; sizes; timings. End with **Decisions**, allowed by the design only:
  - FTS5 absent → the Ask tab shows "search ranking uses the LIKE fallback in this browser" (Builder-check 6).
  - A tab's call FAILs → Task 2 precomputes that tab's outputs into the manifest under `precomputed.<tab>`, and the tab shows them with a visible "precomputed at build time: <reason>" note (design Constraints).
  - The wheels cannot be imported at all, or the store cannot be opened → STOP and report to the owner. The design has no live fallback for that, and degrading every tab is an owner decision.
- [ ] **Step 11: Run the gate**, then commit:

```bash
git add scripts/_shop_docs.py scripts/build_playground_store.py site-playground/glue.py scripts/pyodide_check.mjs docs/specs/2026-11-27-pyodide-spike.md tests/conformance/test_playground.py
git commit -m "feat(docs): playground store, browser glue and Pyodide spike"
```

---

### Task 2: Glue for all five tabs

**Files:**
- Modify: `site-playground/glue.py`, `scripts/build_playground_store.py` (manifest additions)
- Test: `tests/conformance/test_playground.py`

**Interfaces:**
- Consumes: Task 1 glue and manifest; `Guru.config` setter (`src/schemaguru/guru.py:337`); `dataclasses.replace` on `Config.sampling`; `schemaguru.render.table_block.predicate_text` (`:422`); `Guru.load_overlays(*paths)`, which replaces the previous load (`guru.py:690`) and so must always be passed the base overlay too.
- Produces (in addition to Task 1):
  - `sg_mask(text: str, mode: str = "masked") -> str` → `{"mode", "text", "hits", "categories": [str], "table_block": str}`. `masked` = `mask_free_text`. `none` = `predicate_text(mask_free_text(text)[0], "none")`, mirroring `ValuePresenter.reason`. `table_block` = `describe_table("shop.customers")` rendered with `sampling.llm_values=mode` (config swapped, then restored). Any other mode → error `bad_mode`.
  - `sg_override(overlay_yaml: str, question: str) -> str` → `{"diagnostics": [{"code", "severity", "message"}], "before": {"text", "fingerprint"}, "after": {"text", "fingerprint"}}`. Writes the YAML to `<store dir>/user_overlay.yaml` and calls `load_overlays(base, user)`.
  - `sg_reset(question: str) -> str` → `{"text", "fingerprint"}`, after `load_overlays(base)` only.
  - `sg_open` also errors with code `version_mismatch` when `manifest["schemaguru_version"] != schemaguru.__version__`.
  - Input limits: question ≤ 500 chars, mask text ≤ 10 000, overlay YAML ≤ 20 000 → error `too_large`. An empty question → error `empty`. `profile` ∉ `{"agent","planner","verifier","router"}` → `bad_profile`. `depth` is clamped by the library (1..5); out of range → the library's own error.
  - Manifest additions: `"privacy_examples": [{"label": str, "text": str}]` (synthetic email, phone, IBAN `GB82 WEST 1234 5698 7654 32`, Luhn test card `4111 1111 1111 1111`, a `password=` key/value, a quoted literal), `"override_example": str` (YAML: a description and a pitfall on `shop.customers`, plus a `lineage` `ignore: true` on `analytics.paid_revenue`), and `"precomputed": {}` filled only per a Task 1 FAIL decision.

- [ ] **Step 1: Write the failing tests.**

```python
def test_glue_errors_are_envelopes(opened):            # Review Focus 2
    for raw in (opened["sg_context"](""), opened["sg_context"]("x" * 501),
                opened["sg_path"]("shop.nope", "shop.orders"),
                opened["sg_lineage"]("shop.nope", "upstream", 2),
                opened["sg_context"]("q", "bogus"), opened["sg_mask"]("a", "raw")):
        out = json.loads(raw)
        assert set(out["error"]) >= {"code", "message"} and out["text"]
    bad = json.loads(opened["sg_override"]("tables: [unclosed", "orders"))
    assert bad["diagnostics"] and bad["diagnostics"][0]["code"].startswith("SG")

def test_override_wins_and_reset_restores(opened):     # Review Focus 3
    q = "active customers by segment"
    first = json.loads(opened["sg_context"](q))["fingerprint"]
    yaml_text = ("version: 1\nwarehouse: shop\ntables:\n  shop.customers:\n"
                 "    description: Playground override wins.\n")
    out = json.loads(opened["sg_override"](yaml_text, q))
    assert "Playground override wins." in out["after"]["text"]
    assert out["after"]["fingerprint"] != out["before"]["fingerprint"]
    assert json.loads(opened["sg_reset"](q))["fingerprint"] == first

def test_mask_modes(opened):
    s = "ping jane.doe@example.org about status = 'vip'"
    m, n = json.loads(opened["sg_mask"](s, "masked")), json.loads(opened["sg_mask"](s, "none"))
    assert "jane.doe@example.org" not in m["text"] and "email" in m["categories"]
    assert "'vip'" in m["text"] and "'vip'" not in n["text"] and "?" in n["text"]
    assert m["table_block"] != n["table_block"]

def test_version_mismatch_is_refused(playground, glue): ...  # manifest with "0.0.0" -> code version_mismatch

def test_privacy_examples_are_synthetic(playground):
    m = json.loads((playground / "playground.json").read_text())
    for ex in m["privacy_examples"]:
        assert not re.search(r"\b(ghp_|gho_|github_pat_|sk_live_|sk-|AKIA|AIza|xox[abp]-)", ex["text"])
        assert json.loads(GLUE["sg_mask"](ex["text"], "masked"))["hits"] >= 1  # each example masks
```

If the `'vip'` assertion in `test_mask_modes` contradicts the real `mask_free_text` (e.g. it masks quoted literals in `masked` mode), assert the real behaviour and record it. The library stays unchanged.

- [ ] **Step 2: Run them and confirm they fail.** Run `pytest tests/conformance/test_playground.py -v -m "not integration"`. Expected: FAIL (`KeyError: 'sg_override'`, missing `categories`, missing manifest keys).
- [ ] **Step 3: Implement** the Interfaces above in `glue.py` and the manifest additions in `build_playground_store.py`. Extend `smoke()` so it calls every entry point once, including override + reset.
- [ ] **Step 4: Run the tests and confirm they pass.** Run `pytest tests/conformance/test_playground.py -v -m "not integration"`, then PREP and `node scripts/pyodide_check.mjs …` as in Task 1 Step 9. Expected: PASS, and `ok …` from Pyodide.
- [ ] **Step 5: Run the gate**, then commit `site-playground/glue.py scripts/build_playground_store.py tests/conformance/test_playground.py` with the message `feat(docs): playground glue for context, paths, lineage, privacy and overrides`.

---

### Task 3: Visual system

**Files:**
- Create: `docs/assets/schemaguru.css`, `docs/assets/schemaguru.js`, `docs/assets/favicon.svg` (own simple mark, no third-party art)
- Delete: `docs/assets/extra.css` (move its 3 rules into `schemaguru.css`)
- Modify: `mkdocs.yml` (`theme.custom_dir: docs/overrides`, `exclude_docs` + `overrides/`, `theme.font: false`, `theme.favicon`, palette `primary: custom` / `accent: custom` in both schemes, `extra_css: [assets/schemaguru.css]`, `extra_javascript: [assets/schemaguru.js]`); create an empty `docs/overrides/.gitkeep` only if mkdocs requires the dir to exist before Task 5
- Test: `tests/conformance/test_site_theme.py`

**Interfaces:**
- Produces, for Tasks 5–7:
  - CSS tokens on `[data-md-color-scheme="default"]` and `[data-md-color-scheme="slate"]`: `--sg-bg`, `--sg-surface`, `--sg-fg`, `--sg-fg-muted`, `--sg-border`, `--sg-indigo`, `--sg-teal`, `--sg-amber`, `--sg-sky`, `--sg-rose`, `--sg-accent-ink` (text on an accent fill), `--sg-code-bg`. The light palette follows the brief: indigo `#4f46e5`, teal `#0d9488`, amber `#d97706`, sky `#0ea5e9`, rose `#e11d48` on slate (`#0f172a` text, `#f8fafc` bg). Exact dark values are the builder's choice, within AA. Map `--md-primary-fg-color` and `--md-accent-fg-color` onto them.
  - Components (class names): `.sg-section`, `.sg-lead`, `.sg-button` / `.sg-button--solid`, `.sg-chip` (copy chip with `data-copy="<text>"`), `.sg-cards` / `.sg-card`, `.sg-badge`, `.sg-callout`, `.sg-tabs` (with `[role=tablist]` / `[role=tab]` / `[role=tabpanel]`), `.sg-reveal` (fade-up on scroll).
  - `schemaguru.js` (no dependencies, IIFE, runs on `DOMContentLoaded`):
    - `.sg-reveal` gets `.is-visible` via `IntersectionObserver`; immediately when `matchMedia("(prefers-reduced-motion: reduce)")` matches or IO is missing.
    - `[data-copy]` buttons copy to the clipboard and announce "Copied" in an `aria-live` region.
    - `[role=tablist]` gets roving tabindex: Left/Right/Home/End move and activate, and `aria-selected` / `hidden` stay in sync.
  - `@media (prefers-reduced-motion: reduce)` disables every `.sg-*` transition and animation. Layout is fluid down to 360 px.

- [ ] **Step 1: Write the failing tests.**

```python
def contrast(a: str, b: str) -> float: ...   # WCAG 2.x relative-luminance ratio of two #rrggbb

@pytest.mark.parametrize("scheme", ["default", "slate"])
@pytest.mark.parametrize("fg,bg", [("--sg-fg", "--sg-bg"), ("--sg-fg-muted", "--sg-bg"),
    ("--sg-fg", "--sg-surface"), ("--sg-fg-muted", "--sg-surface"), ("--sg-accent-ink", "--sg-indigo")])
def test_text_tokens_meet_aa(scheme, fg, bg):
    tokens = scheme_tokens(CSS, scheme)          # parse the [data-md-color-scheme=...] block
    assert contrast(tokens[fg], tokens[bg]) >= 4.5

def test_negative_control_contrast():
    assert contrast("#777777", "#888888") < 4.5 and contrast("#000000", "#ffffff") == pytest.approx(21)

def test_theme_wiring():
    cfg = yaml.safe_load(...)   # mkdocs.yml with the !!python tags ignored
    assert cfg["theme"]["font"] is False and cfg["theme"]["custom_dir"] == "docs/overrides"
    assert "assets/schemaguru.css" in cfg["extra_css"] and "assets/extra.css" not in cfg["extra_css"]
```

- [ ] **Step 2: Run them and confirm they fail.** Run `pytest tests/conformance/test_site_theme.py -v`. Expected: FAIL (no `schemaguru.css`).
- [ ] **Step 3: Write `schemaguru.css`, `schemaguru.js` and `favicon.svg`; edit `mkdocs.yml`; delete `extra.css`.** Keep `.sg-out` and `.sg-grid`, which existing pages use.
- [ ] **Step 4: Run the tests and the build.** Run `pytest tests/conformance/test_site_theme.py -v && NO_MKDOCS_2_WARNING=1 mkdocs build --strict`. Expected: PASS, and the build has no warnings.
- [ ] **Step 5: Run the gate**, then commit the listed paths (`git rm docs/assets/extra.css`) with the message `feat(docs): SchemaGuru visual system (palette, components, motion)`.

---

### Task 4: Build-time landing data

**Files:**
- Modify: `scripts/gen_docs.py` (new `landing_data(shop: ShopGuru) -> dict[str, Any]` and `scale_claim() -> str`, both called from `generate()`), `scripts/mkdocs_hooks.py` (add `on_env`)
- Create: `docs/generated/landing.json`, `docs/generated/scale_claim.md` (written by the generator, never by hand)
- Test: `tests/conformance/test_landing.py`; `tests/conformance/test_docs_generated.py` already guards currency and orphans

**Interfaces:**
- Consumes: `_shop_docs.shop_guru`, `naive_counts`, `schema_dump`, `block`; `guru.handle_tool_call("find_join_path", …)`; `schemaguru.catalog.registry.dialects()` (`src/schemaguru/catalog/registry.py:44`); `benchmarks/results/0.2.0.json`; `mask_free_text`.
- Produces: `landing.json` (`json.dumps(..., indent=1, sort_keys=True, ensure_ascii=False)`):
  - `question` = `"monthly revenue by customer segment"`.
  - `before` = `shop.before.context(q).text`, the crawled-only view (names and types only).
  - `after_lines` = `[{"text": str, "trap": str | None}]` from `guru.context(q).text`.
  - `before_tokens`, `after_tokens`, `schema_dump_tokens` (`guru.counter.count`).
  - `traps` = list of `{"id", "title", "naive", "evidence", "source"}` for ids `hub` (`find_join_path` orders→events text, refusal `HUB_PIVOT`, naive = the row multiplication from `naive_counts`), `soft_delete` (`NOT deleted` count vs live), `case_drift` (exact vs any-case paid) and `broken_join` (`find_join_path` `shop.broken_child`→`shop.ref_parent`). `evidence` is an exact substring of its source text.
  - `readers` = `dialects()`; `tools` = tool names; `profiles` = the four profile names.
  - `value_policy` = `Config().sampling.llm_values`.
  - `pii_example` = `{"in": <synthetic>, "out": mask_free_text(in)[0]}`.
  - `scale` = `{"tables": int, "context_p95_s": float}` (from the JSON, rounded to 0.1).
  - `schemaguru_version`.
- `scale_claim.md`: one sentence stating the `context` p95 at the benchmark size, linking `guide/scale.md`, formatted the way `tests/conformance/test_scale_page.py:fmt` formats seconds.
- `mkdocs_hooks.on_env(env, config, files)` sets `env.globals["landing"]` to the parsed `landing.json`. If the file is missing, it calls `log.warning(...)` so the strict build fails.

- [ ] **Step 1: Write the failing tests** in `tests/conformance/test_landing.py` (they load `gen_docs` the way `test_docs_generated._load` does):

```python
def test_landing_traps_are_evidenced(landing):
    ids = [t["id"] for t in landing["traps"]]
    assert ids == ["hub", "soft_delete", "case_drift", "broken_join"]
    after = "\n".join(line["text"] for line in landing["after_lines"])
    for t in landing["traps"]:
        assert t["evidence"] and t["naive"]
        if t["source"] == "context":
            assert t["evidence"] in after
            assert any(line["trap"] == t["id"] for line in landing["after_lines"])

def test_landing_counts_match_the_warehouse(landing, shop_counts):
    hub = next(t for t in landing["traps"] if t["id"] == "hub")
    assert f"{shop_counts['orders_x_accounts_x_events']:,}" in hub["naive"]

def test_landing_lists_every_reader(landing):
    assert landing["readers"] == sorted(registry.dialects()) and len(landing["readers"]) == 11

def test_landing_holds_no_raw_identifying_values(landing_text):
    assert not EMAIL.search(landing_text.replace(PII_EXAMPLE_IN, ""))  # only the declared input
    assert not HEX32.search(landing_text)

def test_on_env_without_landing_warns(tmp_path, monkeypatch, caplog): ...  # point the hook at tmp_path
```

Move `EMAIL`/`HEX32` and their negative control into a shared helper only if Task 8 would otherwise orphan them. Otherwise import them from `test_docs_generated`.

- [ ] **Step 2: Run them and confirm they fail.** Run `pytest tests/conformance/test_landing.py -v`. Expected: FAIL (`landing.json` missing, `AttributeError: landing_data`).
- [ ] **Step 3: Implement `landing_data`, `scale_claim` and `on_env`; run `python3 scripts/gen_docs.py`.**
- [ ] **Step 4: Run the tests.** Run `pytest tests/conformance/test_landing.py tests/conformance/test_docs_generated.py -v && NO_MKDOCS_2_WARNING=1 mkdocs build --strict`. Expected: PASS.
- [ ] **Step 5: Run the gate**, then commit `scripts/gen_docs.py scripts/mkdocs_hooks.py docs/generated/landing.json docs/generated/scale_claim.md tests/conformance/test_landing.py` with the message `feat(docs): generate the landing page's before/after and trap data`.

---

### Task 5: Landing page

**Files:**
- Create: `docs/overrides/home.html`, `tests/site/__init__.py`, `tests/site/test_site_smoke.py`
- Rewrite: `docs/index.md` (front matter `template: home.html`, `hide: [navigation, toc]`)
- Test: `tests/conformance/test_landing.py` (template checks), `tests/site/test_site_smoke.py` (integration)

**Interfaces:**
- Consumes: `landing` global (Task 4); CSS/JS components (Task 3).
- Produces: `home.html` extends `main.html`, mirroring `~/factlang/docs/overrides/home.html`.
  - `{% block tabs %}` renders these sections in order, each a `<section class="sg-section sg-reveal" aria-labelledby=…>`:
    1. Hero: one-line promise, sub-line, "Try the playground" → `playground/`, "Get started" → `getting-started/`, and a `pip install schemaguru` `[data-copy]` chip.
    2. Before/after: two `<pre>` panes from `landing.before` and `landing.after_lines` (a trap line wrapped in `<mark data-trap="{{ id }}">`), each pane with its token count, then the four trap callouts from `landing.traps`.
    3. Six benefit cards (D1 list, copy from the builder, figures only from `landing`).
    4. How it works: three steps, crawl & measure → resolve & scope → serve to agents, as an inline SVG with `role="img"` and `<title>`, colours via `currentColor` / CSS vars.
  - `{% block content %}` = `{{ super() }}` (the index.md body), then the supported-warehouses strip (`landing.readers` as `.sg-badge` text), the trust strip (open source MIT, read-only by design, deterministic, tested; link `guide/scale/`; no figures), and the closing CTA.
  - `docs/index.md` body: `## Use it from` with `=== "Python"`, `=== "MCP"`, `=== "CLI"`, `=== "LangChain / LlamaIndex / OpenAI Agents"`, `=== "HTTP server"` tabs, then a short `## Install`. The Python tab is a ```` ```python ```` block, so it runs in `test_docs_examples` against the shop workdir (`warehouse.duckdb`, `schemaguru.yaml`). The framework tab copies the ```` ```py ```` snippets from `docs/guide/agents.md:68-130`. The CLI/MCP/HTTP tabs are ```` ```bash ```` using the commands documented in `docs/reference/cli.md` (`context`, `path`, `mcp`, `serve`). The current problem stories, trap table, "where it shines" and scale paragraph leave the home page. Task 7 moves them to Features.

- [ ] **Step 1: Write the failing tests.**

```python
def test_home_template_has_no_literal_numbers():
    text = strip_jinja_tags_and_attrs(HOME.read_text())   # drop {{ }}, {% %}, {# #}, tags, <style>, <svg>
    assert not re.search(r"\d{2,}|\d[.,]\d|%", text), "numbers must come from landing.json"

def test_home_sections_in_order():
    ids = re.findall(r'<section[^>]*id="([^"]+)"', HOME.read_text())
    assert ids == ["sg-hero", "sg-before-after", "sg-benefits", "sg-how", "sg-warehouses",
                   "sg-trust", "sg-cta"]

def test_negative_control_literal_number():
    assert re.search(r"\d{2,}", strip_jinja_tags_and_attrs("<p>saves 42 tokens</p>"))
```

In `tests/site/test_site_smoke.py` (module marked `integration`; `pytest.importorskip("playwright.sync_api")`; skip unless `site/index.html` exists; serves `site/` with `http.server.ThreadingHTTPServer` on port 0):

```python
def test_landing_narrow_dark_reduced_motion(page, base):     # Review Focus 5
    page.set_viewport_size({"width": 360, "height": 780})
    page.emulate_media(color_scheme="dark", reduced_motion="reduce")
    page.goto(base)
    assert page.evaluate("document.documentElement.scrollWidth") <= 360
    assert page.locator(".sg-reveal").first.evaluate("e => getComputedStyle(e).opacity") == "1"
    assert page.locator("#sg-before-after mark[data-trap]").count() >= 2
    assert page.locator("#sg-warehouses .sg-badge").count() == 11

def test_tabs_keyboard(page, base):    # ArrowRight moves aria-selected within the integration tabs
```

- [ ] **Step 2: Run them and confirm they fail.** Run `pytest tests/conformance/test_landing.py -v`. Expected: FAIL (no `home.html`).
- [ ] **Step 3: Write `home.html` and rewrite `index.md`.** Run `pytest tests/conformance/test_docs_examples.py -q` (the new Python tab runs).
- [ ] **Step 4: Run the tests and the build.** Run `pytest tests/conformance/test_landing.py tests/conformance/test_docs_examples.py -v && NO_MKDOCS_2_WARNING=1 mkdocs build --strict`. Then, if Playwright is available (`.venv/bin/pip install playwright` is allowed as an environment step; never add it to `pyproject.toml`; reuse a cached Chromium in `~/Library/Caches/ms-playwright` or run `python3 -m playwright install chromium`), run `pytest tests/site -m integration -v`. Expected: PASS. If Playwright cannot be installed, say so in the task report.
- [ ] **Step 5: Run the gate**, then commit `docs/overrides/home.html docs/index.md tests/conformance/test_landing.py tests/site/__init__.py tests/site/test_site_smoke.py` with the message `feat(docs): landing page with generated before/after, benefits and integrations`.

---

### Task 6: Playground page

**Files:**
- Create: `docs/playground.md`, `site-playground/playground.js`, `site-playground/playground.css`
- Modify: `scripts/mkdocs_hooks.py` (`on_files` also publishes the playground), `mkdocs.yml` (nav `Playground: playground.md` next to `Demo`; `extra_css`/`extra_javascript` + `assets/playground/playground.{css,js}`)
- Test: `tests/conformance/test_mkdocs_hooks.py`, `tests/site/test_site_smoke.py`

**Interfaces:**
- Consumes: glue entry points (Tasks 1–2), manifest keys, Task 3 components, the Task 1 spike decisions.
- Produces:
  - Hook: every file in `site-playground/` and in `build/playground/` is published under `assets/playground/`. The hook also writes `assets/playground/wheels.json` = `{"schemaguru": <name> | None, "factlang": <name> | None}` (newest `schemaguru-*-py3-none-any.whl` / `factlang-*-py3-none-any.whl`). A missing wheel, `playground.db` or `playground.json` → `log.warning`.
  - `playground.md`: `hide: [navigation, toc]`, an intro paragraph, and `<div id="sg-playground" data-pyodide="https://cdn.jsdelivr.net/pyodide/v0.29.0/full/">` with a `role=tablist` of five tabs: `ask`, `path`, `lineage`, `privacy`, `override`. It has an `aria-live` status line `#sg-pg-status`, a `<noscript>` message, and a `#sg-pg-unsupported` block linking `getting-started/` and the guides. There are no ```` ```python ```` blocks on the page.
  - `playground.js` (IIFE):
    1. If `typeof WebAssembly !== "object"`, show `#sg-pg-unsupported` and stop.
    2. Otherwise start lazily, on the first tab interaction or when `#sg-playground` enters the viewport.
    3. Load in stages, each reported in the status line: Pyodide → `loadPackage(["sqlite3","pyyaml"])` → both wheels from `wheels.json` → store, manifest and overlay written to `/home/pyodide/` → `glue.py` run → `sg_open`.
    4. If any stage fails, show the unsupported block with the stage name and a Retry button.
    5. Results always reach the DOM through `textContent`, never `innerHTML` with result data.
    - Ask: a question input, the four profiles, suggested-question chips from `manifest.questions`, and the token count. When `search_backend == "like"`, show the FTS5 note (Builder-check 6).
    - Join path: two selects from `manifest.tables`, a refusal badge, the path text with its SQL.
    - Lineage: a subject select (`lineage_subjects`), an upstream/downstream toggle, depth 1–5.
    - Privacy: a textarea, a masked/none toggle, example buttons (`privacy_examples`), the masked text, categories and the `shop.customers` block.
    - Override: a YAML textarea prefilled with `override_example`, a question input, Apply/Reset, the diagnostics, and before/after views with their fingerprints. Edits are ephemeral (Builder-check 7).
    - A tab with `manifest.precomputed[tab]` renders that output with the note "precomputed at build time: <reason>".

- [ ] **Step 1: Write the failing tests.**

```python
def test_hook_publishes_playground(tmp_path, monkeypatch):
    # fake build/playground with schemaguru-9.9.9-py3-none-any.whl, factlang-0.1.4-py3-none-any.whl,
    # playground.db, playground.json; call on_files(Files([]), load_config("mkdocs.yml"))
    assert {"assets/playground/glue.py", "assets/playground/playground.db",
            "assets/playground/wheels.json"} <= {f.src_uri for f in files}
    assert json.loads(wheels_json) == {"schemaguru": "schemaguru-9.9.9-py3-none-any.whl",
                                       "factlang": "factlang-0.1.4-py3-none-any.whl"}

def test_hook_warns_without_wheels(tmp_path, monkeypatch, caplog):
    assert "playground cannot load" in caplog.text
```

In `tests/site/test_site_smoke.py`:

```python
def test_playground_runs_every_tab(page, base):            # needs network for the Pyodide CDN
    page.goto(base + "playground/"); wait for "#sg-pg-status" text "Ready" (timeout 180 s)
    # ask: a suggested question -> output contains "shop.orders"; path orders->events -> "HUB_PIVOT";
    # lineage revenue_by_segment upstream 3 -> "shop.payments"; privacy example -> no "@example";
    # override apply -> "Playground override wins." then reset -> before text restored

def test_playground_without_webassembly_explains(page, base):   # Review Focus 1
    page.add_init_script("delete window.WebAssembly")
    page.goto(base + "playground/")
    assert page.locator("#sg-pg-unsupported").is_visible()      # within 5 s, no network needed

def test_pasted_html_is_inert(page, base):                       # Review Focus 4
    # after Ready: privacy textarea <- '<img src=x onerror="window.__pwned=1">'; run
    assert page.evaluate("window.__pwned") is None
    assert page.locator("#sg-playground img[src='x']").count() == 0
```

- [ ] **Step 2: Run them and confirm they fail.** Run `pytest tests/conformance/test_mkdocs_hooks.py -v`. Expected: FAIL (no playground publishing).
- [ ] **Step 3: Implement the hook changes, `playground.md`, `playground.js`, `playground.css` and the nav entry.**
- [ ] **Step 4: Run PREP, then the tests and the build.** Run `pytest tests/conformance/test_mkdocs_hooks.py -v && NO_MKDOCS_2_WARNING=1 mkdocs build --strict && pytest tests/site -m integration -v`. Expected: PASS (or the site tests skipped, reported as not available).
- [ ] **Step 5: Run the gate**, then commit `docs/playground.md site-playground/playground.js site-playground/playground.css scripts/mkdocs_hooks.py mkdocs.yml tests/conformance/test_mkdocs_hooks.py tests/site/test_site_smoke.py` with the message `feat(docs): Pyodide playground with five live tabs`.

---

### Task 7: Features page and architecture diagram

**Files:**
- Create: `docs/features.md`
- Modify: `mkdocs.yml` (nav `Features: features.md` after Home)
- Test: `tests/conformance/test_landing.py` (features checks), `tests/site/test_site_smoke.py`

**Interfaces:**
- Consumes: `docs/generated/scale_claim.md` (Task 4) via `--8<--`; the stories `generated/story_hub.md`, `story_soft_delete.md`, `story_case_drift.md` and the trap table, which leave the old index; Task 3 components.
- Produces: `features.md` has one scannable grid of out-of-the-box capabilities. Each item is a `.sg-card` with a one-line claim and a link to its guide: crawl, measure, sampling, join graph + refusals, PII masking, overlays, lineage, scoping, rendering profiles, refresh/drift, runtime feedback, agents/tools/MCP, server, drafting with an LLM (optional), extending. Then the three generated stories and the trap table moved from the old index. Then the scale paragraph built around `--8<-- "generated/scale_claim.md"`, so the known limit stays visible next to the scale claim (D5). Then `## Architecture`: an inline `<svg role="img" aria-labelledby>` with `<title>`/`<desc>` drawing warehouse → catalog readers → store ← measure (FactLang) / overlays (YAML) / lineage → graph + scope → render → agents (prompt, tools, MCP, adapters, HTTP). Colours come only via `currentColor` and `var(--sg-*)`, so both themes work. A `<details>` text equivalent follows it.

- [ ] **Step 1: Write the failing tests.**

```python
def test_features_link_every_core_guide():
    links = set(re.findall(r"\]\((guide/[a-z-]+\.md)", FEATURES.read_text()))
    assert {f"guide/{g}.md" for g in ("crawl", "measure", "sampling", "graph", "pii", "overlays",
            "lineage", "scoping", "rendering", "refresh", "feedback", "agents", "server",
            "drafting")} <= links

def test_features_diagram_is_accessible_and_theme_safe():
    svg = re.search(r"<svg.*?</svg>", FEATURES.read_text(), re.S).group(0)
    assert 'role="img"' in svg and "<title" in svg
    assert not re.search(r'(fill|stroke)="#[0-9a-fA-F]{3,6}"', svg)   # colours only via CSS vars

def test_features_scale_claim_is_generated():
    assert '--8<-- "generated/scale_claim.md"' in FEATURES.read_text()
    assert "28.6" not in FEATURES.read_text()                           # never hand-typed
```

In the site smoke test, add `test_features_diagram_both_themes`: the SVG's computed stroke differs between `color_scheme="light"` and `"dark"`, and there is no horizontal scroll at 360 px.

- [ ] **Step 2: Run them and confirm they fail.** Run `pytest tests/conformance/test_landing.py -k features -v`. Expected: FAIL (no `features.md`).
- [ ] **Step 3: Write `features.md` and add the nav entry.**
- [ ] **Step 4: Run the tests and the build.** Run `pytest tests/conformance/test_landing.py tests/conformance/test_docs_examples.py -v && NO_MKDOCS_2_WARNING=1 mkdocs build --strict` (after PREP). Expected: PASS.
- [ ] **Step 5: Run the gate**, then commit `docs/features.md mkdocs.yml tests/conformance/test_landing.py tests/site/test_site_smoke.py` with the message `feat(docs): Features page with architecture diagram`.

---

### Task 8: Retire the demo, update docs.yml, tidy the nav

**Files:**
- Delete: `docs/demo.md`, `site-demo/demo.js`, `site-demo/demo.css`, `site-demo/shop_export.json`, `scripts/build_demo_export.py`
- Modify:
  - `tests/conformance/test_docs_generated.py`: drop the `export_text` fixture and the three export tests. Keep `EMAIL`/`HEX32` and the negative control if Task 4 imports them. Fix the module docstring.
  - `scripts/mkdocs_hooks.py`: drop `site-demo` publishing and fix the docstring.
  - `scripts/_shop_docs.py:3-4`: the docstring now names `build_playground_store.py`.
  - `mkdocs.yml` nav: `Home`, `Features`, `Playground`, `Getting started`, `Guide`, `Extending`, `Reference`, `Examples`; remove `Demo`.
  - `README.md:150`: "interactive demo" → "in-browser playground".
  - `CHANGELOG.md`: an `Unreleased` "Documentation" entry; the `docs/demo.md` link at `:96` now points to `docs/playground.md`.
  - `.github/workflows/docs.yml`.
- Test: the existing conformance suite; `grep` sweep

**Interfaces:**
- Consumes: PREP commands (Global Constraints), `site-playground/glue.py` `__main__`.
- Produces: the `docs.yml` steps, in order:
  1. checkout, then setup-python 3.12.
  2. `pip install build "mkdocs-material" "mkdocstrings[python]"`.
  3. `python -m build --wheel --outdir build/playground`.
  4. `pip install "$(ls build/playground/schemaguru-*.whl)[duckdb,lineage]"`.
  5. `pip download "factlang==$(python -c 'import importlib.metadata as m; print(m.version("factlang"))')" --no-deps --only-binary=:all: -d build/playground`.
  6. `python scripts/build_playground_store.py --out build/playground`.
  7. Smoke-test the glue in a clean venv: `python -m venv /tmp/smoke && /tmp/smoke/bin/pip install build/playground/*.whl && cd /tmp && /tmp/smoke/bin/python -I "$GITHUB_WORKSPACE/site-playground/glue.py" "$GITHUB_WORKSPACE/build/playground"`.
  8. `python scripts/gen_docs.py && git diff --exit-code -- docs/`.
  9. `mkdocs build --strict`.
  10. Deploy (unchanged).

  The wheel goes to `--outdir build/playground`, so `dist/` is no longer used by docs.yml.

- [ ] **Step 1: Write the failing check.** Add `test_no_demo_consumers` to `tests/conformance/test_landing.py`. It asserts that no tracked file outside `docs/specs/` mentions `site-demo`, `shop_export.json`, `build_demo_export` or `demo.md`. List the files with `git ls-files`; `CHANGELOG.md` is allowed to keep the word "demo" in prose but no `demo.md` link.
- [ ] **Step 2: Run it and confirm it fails.** Run `pytest tests/conformance/test_landing.py::test_no_demo_consumers -v`. Expected: FAIL, listing `mkdocs.yml`, `docs.yml`, `scripts/…`, `tests/…`, `README.md`.
- [ ] **Step 3: Make the deletions and edits above.** Use `git rm` for the deleted paths.
- [ ] **Step 4: Run the check, the conformance suite and the build.** Run PREP, then `pytest tests/conformance -v -m "not integration" && NO_MKDOCS_2_WARNING=1 mkdocs build --strict`. Expected: PASS. Then replay the docs.yml steps 3–8 locally, with `$SCRATCH` in place of `/tmp` and `$PWD` in place of `$GITHUB_WORKSPACE`. Expected: `ok …`, and the docs diff is clean.
- [ ] **Step 5: Run the gate**, then commit the listed paths with the message `docs: replace the demo with the playground; build the playground in docs.yml`.

---

### Task 9: Final acceptance

**Files:** none new, except fixes to the owning task's files if a check fails (each fix gets its own red test first).

- [ ] **Step 1: Run the gate on a clean build.** Run PREP, then `ruff check && ruff format --check && mypy --strict src && pytest -m "not integration and not live and not benchmark" && NO_MKDOCS_2_WARNING=1 mkdocs build --strict`. Expected: all green, no mkdocs warnings.
- [ ] **Step 2: Run the conformance suite by name.** Run `pytest tests/conformance -v -m "not integration"`. Expected: `test_docs_examples`, `test_docs_generated`, `test_wiki_links`, `test_wiki_facts`, `test_scale_page`, `test_diagnostics_doc`, `test_playground`, `test_landing`, `test_site_theme` and `test_mkdocs_hooks` all pass.
- [ ] **Step 3: Run the real-Pyodide harness against the built site's assets.** Run `node scripts/pyodide_check.mjs "$SCRATCH/pyodide/node_modules/pyodide" site/assets/playground`. Expected: `ok <version> <backend>`.
- [ ] **Step 4: Run the headless smoke.** Run `pytest tests/site -m integration -v` against `site/`. Expected: every test passes, including `test_playground_runs_every_tab` (network to the Pyodide CDN). If Playwright/Chromium cannot be made available locally, report "headless smoke not run: <reason>"; do not claim it.
- [ ] **Step 5: Check the integration suite and the denylist.** Run `pytest tests/conformance/test_playground.py -m integration -v` (clean-venv glue). Run `pytest tests/architecture -v` (denylist; it skips without `SCHEMAGURU_DENYLIST`, so say so).
- [ ] **Step 6: Check the payload size and the dev environment.** Report the transfer size of the playground's own files (two wheels + store) and confirm the store is under 20 MiB. Confirm `git status` shows no stray files. The `.venv` Playwright install is an environment change, not a repo change.
- [ ] **Step 7: Report.** List every command with its result, and the spike decisions that took effect. Commit nothing unless a fix was needed.

---

## Self-review notes (done)

- **Spec coverage.** D1 → Tasks 4–5. D2 → Tasks 1, 2, 6. D3 → Task 7. D4 → Task 3, with Task 5/7 smoke tests. D5 → Tasks 4 and 7 (generated numbers, scale limit next to the scale claim). Constraints: strict build and conformance → every task and Task 9; same-commit wheel → PREP and docs.yml; lineage precomputed → Task 1; spike → Task 1. The denylist, CI time (no new heavy steps) and Builder-check answers 1–7 → Tasks 1, 8, 1, 2, 5, 6 and 6.
- **Type consistency.** The glue names (`sg_open`, `sg_context`, `sg_path`, `sg_lineage`, `sg_mask`, `sg_override`, `sg_reset`, `smoke`), the manifest keys and the `landing.json` keys match across Tasks 1–7.
