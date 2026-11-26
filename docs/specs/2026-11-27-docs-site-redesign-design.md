# SchemaGuru docs site redesign — Design

Status: approved by the owner 2026-11-27 (direction and design). This document is the
binding authority for the docs-site plan.

## Intent
The published site (https://rupakc.github.io/schemaguru/, mkdocs-material, built by
`.github/workflows/docs.yml` into gh-pages) reads as a reference manual. The owner wants it
polished, professional, engaging, interactive and enticing: a visitor should understand in
under a minute **why** SchemaGuru exists, **what it does out of the box**, and **see it work**
in a playground. Inspiration: the FactLang site (custom mkdocs-material home via
`docs/overrides/home.html`, `factlang.css`/`factlang.js`, a Pyodide playground running the
real wheel in the browser; repo `~/factlang/docs`) and the Storming Bastille landing page
(`~/storming-bastille/docs/index.html`: hero, light palette indigo/teal/amber/sky/rose on
slate, fade-up motion, feature sections, a closing call to action).

## Goals
- **D1 Landing page** (custom home template replacing the prose home), in this order:
  hero (one-line promise, sub-line, primary CTA "Try the playground", secondary "Get
  started", `pip install schemaguru` copy chip); a **before/after** panel — what an agent
  sees from names only vs SchemaGuru's context view for the same question on the shop
  warehouse, highlighting the real traps (a join that multiplies rows, a TRUE-or-NULL
  soft-delete flag, an enum with a case variant, a broken join); **six benefit cards**:
  measured joins and refused row-multiplying paths, values and pitfalls an agent would miss,
  PII masked by default, lineage (dbt + view SQL), humans override in reviewable YAML,
  works without an LLM; a **3-step how-it-works** diagram (crawl & measure → resolve &
  scope → serve to agents); **integration tabs** (Python, MCP, CLI, LangChain/LlamaIndex/
  OpenAI Agents, HTTP server) with real snippets; **supported warehouses** strip (the 11
  readers, text badges — no third-party logos/trademark artwork); a **trust** strip (open
  source MIT, read-only by design, determinism, tested numbers); a closing CTA.
- **D2 Playground page** running the **real SchemaGuru wheel in the browser** via Pyodide
  (as FactLang does), on a small knowledge store of the **shop** fixture built in CI from the
  same commit. Tabs:
  1. *Ask a question* — `guru.context(question)` live, profile switch agent/planner/verifier/
     router, token count, suggested questions.
  2. *Join path* — pick two tables → `find_join_path` result incl. typed refusals and the SQL
     snippet.
  3. *Lineage* — pick a table/column → the `lineage` tool (upstream/downstream, depth),
     from lineage facts precomputed in CI (see Constraints).
  4. *Privacy* — paste any text → the masking sweep live (masked/none modes), with
     examples (emails, IBANs, cards, keys).
  5. *Override* — an editable YAML overlay (e.g. a description, a pitfall, an ignore) →
     apply → the context view updates; human always wins.
  Loads lazily, shows progress, degrades to a clear message without WebAssembly; no network
  calls except the Pyodide CDN and the site's own files.
- **D3 Features page** — one scannable page of what works out of the box, each item linking
  to its guide; plus an **architecture diagram** (inline SVG, both themes).
- **D4 Visual system** — a SchemaGuru palette and type scale (light + dark), consistent
  cards, callouts, code blocks, buttons, subtle motion that respects
  `prefers-reduced-motion`; responsive down to 360 px; accessible (contrast AA, keyboard,
  aria labels).
- **D5 Truthful** — every number and output shown is produced by the real library on the
  shop fixture (generated at build time or live in the playground); the scale limitation
  (`context` p95 28.6 s at 500k tables) stays visible where scale is claimed; no invented
  benchmarks, logos or testimonials.

## Non-goals
- Live warehouse connections from the browser; a hosted backend.
- Changing library behaviour (docs-only, plus any small read-only helper the playground
  needs, added to the library with tests).
- Replacing the existing guides/reference/wiki content (they stay; navigation is tidied).

## Constraints
- mkdocs-material stays the site generator; `mkdocs build --strict` must pass; the existing
  conformance tests (docs examples run, generated docs current, wiki links/facts) stay green.
- The playground must use the wheel built from the same commit (docs.yml already builds it).
- Lineage parsing runs in a worker **subprocess**, which the browser cannot start: lineage
  facts are computed in CI into the playground store; the playground reads them live with the
  real `lineage` tool.
- Pyodide must provide every runtime dependency: `factlang` (pure Python), `PyYAML`,
  `sqlite3` (Pyodide stdlib package). The builder verifies this first (spike) and records the
  outcome; if a dependency is impossible, the tab degrades to precomputed output with a
  visible note — never a fake.
- No proprietary-predecessor mention (denylist CI job). Commits as the noreply author.
- Docs-site CI time stays reasonable (< 10 min).

## Approaches (decided)
1. **Pyodide + real wheel (chosen)** — truthful, matches FactLang, interactive beyond fixed
   examples. Cost: ~10 MB first load, lineage precomputed.
2. Pure-JS viewer over a precomputed export (today's demo) — fast but fixed outputs; weaker
   "see it work".
3. Hosted backend — needs servers and secrets; out of scope.

## Left to the builder
Exact copy wording, layout details, colour values within the brief, animation details,
which suggested questions/examples to ship, test design for the playground (a headless
check that the page builds and the Python entry points it calls exist and return the
expected shape).

## Builder-check answers (decided 2026-11-27)
1. **Lineage data:** a separate playground fixture = the shop warehouse **plus** a small
   `analytics` layer of DuckDB views (e.g. a revenue-by-segment view with a join and an
   aggregate, a paid-orders view, a view over another view) crawled with lineage on, and
   optionally a tiny dbt manifest. The shop fixture used by goldens, generated docs and tests
   is not changed.
2. **Old demo:** `docs/demo.md` + `site-demo/` are retired and replaced by the playground
   (nav "Demo" → "Playground"); a browser without WebAssembly gets a clear message linking
   the guides. Anything else that consumed `site-demo/shop_export.json` is checked and
   updated.
3. **Store delivery:** built in `docs.yml` from the same commit (CI-only artifact, not
   committed), copied into the site with the two wheels; a test builds it locally and checks
   its shape (the "committed copy is current" check does not apply to it).
4. **Privacy examples:** obviously synthetic values only; no string that matches a real
   provider-key format strongly enough to trip secret scanning (GitHub push protection).
5. **Trust strip:** qualitative claims only (open source, read-only, deterministic, tested),
   with a link to the Scale page for measured numbers; no figures in the strip.
6. **FTS5 in the browser:** the spike checks it; if absent, the playground shows a visible
   note that search ranking uses the LIKE fallback in this browser.
7. **Override tab:** edits are ephemeral (reset on reload).
