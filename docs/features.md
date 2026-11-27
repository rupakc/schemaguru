---
title: Features
description: What SchemaGuru does out of the box, each capability linked to its guide.
hide:
  - navigation
  - toc
---

# Features

What works out of the box in 0.2.1 (alpha). Each card links to its guide. Try it first in the
[playground](playground.md) or read the [landing page](index.md) for the short version.

<div class="sg-cards" markdown>

<div class="sg-card" markdown>

### Crawl the catalog

Reads the system catalog only, never a data row; paged, resumable, with readers for eleven warehouses. [Read the guide](guide/crawl.md)

</div>

<div class="sg-card" markdown>

### Measure what joins

Match rate in both directions, fan-in, observed values and null rates, each with its method and date. [Read the guide](guide/measure.md)

</div>

<div class="sg-card" markdown>

### Sample big tables

Exact on small tables, a labelled representative sample on large ones; never `LIMIT`. [Read the guide](guide/sampling.md)

</div>

<div class="sg-card" markdown>

### Join graph and refusals

Path search over measured joins; a route that would multiply rows is refused, a thin join is labelled. [Read the guide](guide/graph.md)

</div>

<div class="sg-card" markdown>

### PII masking

Values shown to an agent or an LLM are masked by default. [Read the guide](guide/pii.md)

</div>

<div class="sg-card" markdown>

### Overlays in YAML

Humans write meaning, enums, live-rows predicates and trusted joins in reviewable files SchemaGuru never edits. [Read the guide](guide/overlays.md)

</div>

<div class="sg-card" markdown>

### Lineage

What a table or column is built from and what reads it, from view definitions and dbt SQL. [Read the guide](guide/lineage.md)

</div>

<div class="sg-card" markdown>

### Question scoping

A token-budgeted slice of the warehouse for one question, not the whole schema. [Read the guide](guide/scoping.md)

</div>

<div class="sg-card" markdown>

### Rendering profiles

Deterministic, fingerprinted views in the shape each kind of agent reads best. [Read the guide](guide/rendering.md)

</div>

<div class="sg-card" markdown>

### Refresh and drift

Incremental re-crawl and re-measure; every change is judged against the previous snapshot. [Read the guide](guide/refresh.md)

</div>

<div class="sg-card" markdown>

### Runtime feedback

Report a failed query and the next measurement run looks there first. [Read the guide](guide/feedback.md)

</div>

<div class="sg-card" markdown>

### Agents, tools and MCP

The same knowledge as prompt text, tool calls, an MCP server or framework adapters. [Read the guide](guide/agents.md)

</div>

<div class="sg-card" markdown>

### Server mode

One shared knowledge store behind HTTP for several agents and replicas. [Read the guide](guide/server.md)

</div>

<div class="sg-card" markdown>

### Drafting with an LLM (optional)

An LLM drafts meanings; they are stored as drafts, marked as such, and never replace measured facts. [Read the guide](guide/drafting.md)

</div>
<div class="sg-card" markdown>

### Extending

Every component sits behind a small protocol: readers, stores, retrievers, providers, detectors and more. [Extension points](extending/index.md)

</div>

</div>

!!! note "Readers"
    --8<-- "generated/readers_tested.md"

## Why it matters: a wrong guess does not error

An agent that writes SQL against a warehouse knows what the names tell it. Names mislead in
at least three common ways, and each returns a plausible, wrong number instead of an error.
These three come from the shop warehouse SchemaGuru is tested on; the row counts are counted
with plain SQL on that warehouse.

### 1. The wrong join

--8<-- "generated/story_hub.md"

### 2. The soft-delete flag that is never FALSE

--8<-- "generated/story_soft_delete.md"

### 3. The enum that drifted in case

--8<-- "generated/story_case_drift.md"

### How SchemaGuru catches them

| Trap | What SchemaGuru does | Read more |
|---|---|---|
| Row-multiplying join | measures every candidate join in **both directions** (match rate, reverse coverage, fan-in); refuses a route that arrives at a hub on one many-to-one edge and leaves on another (`HUB_PIVOT`) | [Join graph](guide/graph.md) |
| TRUE/NULL flag | measures observed values *and* the null rate; shows the human's live-rows predicate next to them | [Measure](guide/measure.md) |
| Case drift | keeps a declared enum as an *expectation*, never rewrites it; reports what measurement saw beyond it | [Overlays](guide/overlays.md) |
| Narrow join | a thin but correct join is returned **labelled** with its coverage; only broken joins are refused | [Join graph](guide/graph.md) |
| Stale knowledge | refresh re-measures, diffs against the previous snapshot and classifies each change | [Refresh](guide/refresh.md) |

Every statement carries its provenance (*catalog*, *declared*, *measured*, *drafted* by an
LLM, or *human*) and every measured number carries its method and date. Unknown is shown as
unknown, never as 0 or 100%. A view that shows a warning also says that the absence of a
warning is not verification.

## Where it shines

<div class="sg-grid" markdown>
<div markdown>
**Agents on huge warehouses.** Metadata-only, paged, resumable crawls; search runs in the
store's full-text index, so a view is scoped without loading every table. Lazy, budgeted
measurement: an agent's question never spends warehouse money by itself.
</div>
<div markdown>
**Onboarding analysts.** `schemaguru context "<question>"` prints what a newcomer needs:
which tables answer it, which joins are measured, which columns hold traps.
</div>
<div markdown>
**Data-contract CI.** Declare enums, joins and soft-delete rules in YAML; `schemaguru
refresh` exits `1` when a change needs review, and view fingerprints detect prompt drift.
</div>
<div markdown>
**Migration audits.** Measure the same joins before and after a move; the refresh report
says which verdicts changed, judged relative to the previous snapshot.
</div>
</div>

## Scale

Measured on the developer's machine (hardware-dependent): lexical table search over a
synthetic 500,000-table store and graph expansion over 50,000 joins are fast; the whole
`context` call at that size is not.

--8<-- "generated/scale_claim.md"

## Architecture

<div class="sg-arch-scroll" tabindex="0" role="group" aria-label="Architecture diagram, scrolls sideways on narrow screens">
<svg class="sg-arch" viewBox="0 0 1000 340" width="1000" role="img" aria-labelledby="sg-arch-title sg-arch-desc">
<title id="sg-arch-title">SchemaGuru architecture</title>
<desc id="sg-arch-desc">The warehouse feeds catalog readers, which write to the store. Measurement with FactLang, human YAML overlays and lineage also write to the store. The join graph and question scoping read the store, rendering turns the scoped result into a view, and agents receive it as a prompt, tools, an MCP server, framework adapters or HTTP.</desc>
<defs><marker id="sg-arch-head" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0L10 5L0 10z" fill="currentColor"/></marker></defs>
<rect x="20" y="170" width="120" height="60" rx="8"/><text x="80.0" y="205.0" text-anchor="middle">Warehouse</text>
<rect x="180" y="170" width="130" height="60" rx="8"/><text x="245.0" y="205.0" text-anchor="middle">Catalog readers</text>
<rect x="340" y="170" width="120" height="60" rx="8"/><text x="400.0" y="205.0" text-anchor="middle">Store</text>
<rect x="180" y="50" width="120" height="50" rx="8"/><text x="240.0" y="72.0" text-anchor="middle">Measure</text><text class="sub" x="240.0" y="89.0" text-anchor="middle">FactLang</text>
<rect x="330" y="50" width="130" height="50" rx="8"/><text x="395.0" y="72.0" text-anchor="middle">Overlays</text><text class="sub" x="395.0" y="89.0" text-anchor="middle">YAML</text>
<rect x="490" y="50" width="120" height="50" rx="8"/><text x="550.0" y="80.0" text-anchor="middle">Lineage</text>
<rect x="500" y="170" width="140" height="60" rx="8"/><text x="570.0" y="205.0" text-anchor="middle">Graph + scope</text>
<rect x="680" y="170" width="100" height="60" rx="8"/><text x="730.0" y="205.0" text-anchor="middle">Render</text>
<rect x="830" y="98" width="150" height="32" rx="8"/><text x="905.0" y="119.0" text-anchor="middle">Prompt</text>
<rect x="830" y="142" width="150" height="32" rx="8"/><text x="905.0" y="163.0" text-anchor="middle">Tools</text>
<rect x="830" y="186" width="150" height="32" rx="8"/><text x="905.0" y="207.0" text-anchor="middle">MCP</text>
<rect x="830" y="230" width="150" height="32" rx="8"/><text x="905.0" y="251.0" text-anchor="middle">Adapters</text>
<rect x="830" y="274" width="150" height="32" rx="8"/><text x="905.0" y="295.0" text-anchor="middle">HTTP</text>
<line x1="140" y1="200" x2="180" y2="200" marker-end="url(#sg-arch-head)"/>
<line x1="310" y1="200" x2="340" y2="200" marker-end="url(#sg-arch-head)"/>
<line x1="460" y1="200" x2="500" y2="200" marker-end="url(#sg-arch-head)"/>
<line x1="640" y1="200" x2="680" y2="200" marker-end="url(#sg-arch-head)"/>
<line x1="240" y1="100" x2="370" y2="170" marker-end="url(#sg-arch-head)"/>
<line x1="395" y1="100" x2="400" y2="170" marker-end="url(#sg-arch-head)"/>
<line x1="550" y1="100" x2="430" y2="170" marker-end="url(#sg-arch-head)"/>
<line x1="80" y1="170" x2="200" y2="100" stroke-dasharray="5 4" marker-end="url(#sg-arch-head)"/>
<line x1="780" y1="200" x2="830" y2="114" marker-end="url(#sg-arch-head)"/>
<line x1="780" y1="200" x2="830" y2="158" marker-end="url(#sg-arch-head)"/>
<line x1="780" y1="200" x2="830" y2="202" marker-end="url(#sg-arch-head)"/>
<line x1="780" y1="200" x2="830" y2="246" marker-end="url(#sg-arch-head)"/>
<line x1="780" y1="200" x2="830" y2="290" marker-end="url(#sg-arch-head)"/>
</svg>
</div>

<details markdown>
<summary>Text equivalent of the diagram</summary>

1. The **warehouse** is read by **catalog readers**, which write to the **store**.
2. **Measure** (through FactLang), **overlays** (human YAML) and **lineage** also write to
   the store; measurement queries the warehouse.
3. The **join graph** and **question scoping** read the store.
4. **Rendering** turns the scoped result into a view.
5. **Agents** receive it as a prompt, tools, an MCP server, framework adapters or HTTP.

</details>
