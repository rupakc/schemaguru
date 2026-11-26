# Rendering views

A `ContextView` is what an agent reads. It is plain text by default, built from ordered
blocks, fingerprinted, and rendered the same way every time for the same knowledge.

```python
import schemaguru as sg

guru = sg.Guru.connect("duckdb:///warehouse.duckdb")
guru.measure(all=True)
guru.load_overlays("schemaguru.yaml")

view = guru.context("refund rate by month")
print(view.tokens, view.fingerprint[:12], [t.canonical() for t in view.tables])
for block in view.blocks:
    print("stable" if block.stable else "scoped", block.text.splitlines()[0])
print(view.to_markdown()[:200])
print(view.to_json()[:200])
print(view.to_xml()[:200])
print(str(guru.context("refund rate by month", format="markdown"))[:80])
```

* `.text` — the default format; `str(view)` renders the format the view was asked for
  (`format="text" | "markdown" | "json" | "xml"`, or one you [register](../extending/profile-format.md)).
* `.blocks` — ordered `ViewBlock(stable, text)`: the blocks that do not depend on the
  question (global pitfalls, the never-query list) are marked `stable` and come first, so a
  host can put a prompt-cache breakpoint after them; the legend, glossary hits and table
  blocks follow.
* `.tables`, `.tokens`, `.fingerprint` (SHA-256 of the text), `.diagnostics` (for example
  `SG505`, how many values the value policy masked).

## Profiles

Different agents need different shapes. Four profiles ship (`profile=`); the set is open.

=== "agent (default)"

    Executor-style detail: every column with its measured facts and warnings.

    ```text
    --8<-- "generated/profile_agent.txt"
    ```

=== "planner"

    Tables, what they answer and join paths; few columns.

    ```text
    --8<-- "generated/profile_planner.txt"
    ```

=== "verifier"

    Keyed, parseable lines (`COLUMNS`, `VALUES`, `JOIN`, `PK`, …) for a checker model. A table with
    [lineage](lineage.md) may carry several `LINEAGE` lines, one per distinct link:
    `LINEAGE <table> <- <ref>[, <ref>…]`, and `COLUMN_LINEAGE <table>.<column> <- <ref>[, <ref>…]
    <transform>`. A ref the store does not hold is written `name(unresolved)`, with no space.

    ```text
    --8<-- "generated/profile_verifier.txt"
    ```

=== "router"

    Table names and one-line descriptions only, for classification.

    ```text
    --8<-- "generated/profile_router.txt"
    ```

Lineage appears in two of them: the `agent` profile adds `Built from: …` under the description and
`← input (transform)` after a column; `planner` carries the table-level line only; `router` is
unchanged. Every lineage line says it is declared or parsed and unverified ([details](lineage.md#what-agents-see)).

(Each is `guru.context("refund rate by month", profile=..., budget_tokens=1200)` on the
measured shop with its overlay.)

## Rendering rules

* **Consequence order.** Warnings come before confirmations, and truncation drops
  confirmations first, never warnings. A hedge is placed above what it qualifies.
* **Numbers keep their method.** `measured <date>`, `verified <date>`, `≈ … (sampled f%, …)`,
  `≥ … (lower bound)`; a thin join states its coverage as a number.
* **Drafts are marked** *(draft)*; human statements not yet checked are marked
  *(human, unchecked)*.
* **Values lists are capped** (`ScopeConfig.max_values_shown`, 6) with "+N more".
* **The legend** appears only when a `⚠` does, and it always says: *absence of a warning is
  not verification*.

## Fingerprints for CI

`guru.fingerprint_views(questions)` renders a fixed question set and returns
`{question: sha256}`. Commit it; after a refresh, a changed fingerprint means the prompt your
agent reads has changed — review it like code.

```python
prints = guru.fingerprint_views(["refund rate by month", "events per account and session"])
print({q: h[:12] for q, h in prints.items()})
guru.close()
```
