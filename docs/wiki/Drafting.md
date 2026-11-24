# Drafting

> Drafting asks an LLM to write the *meaning* of a table: a description, column descriptions, use cases, the entity it holds, pitfalls, a soft-delete guess, join candidates and glossary terms.
> Drafts are stored as `DRAFTED` facts marked "(draft)" in every view; their checkable parts become FactLang claims and are verified, and a person's words always win over a draft.
> Drafting is optional, runs only when you ask (never on an agent's question), sees data values only through the masking policy, and is capped by a token budget.

## Running a draft

```python
provider = sg.draft.AnthropicProvider()            # extra: schemaguru[anthropic]
report = guru.draft(["shop.*"], provider=provider)  # names or globs
report.drafted, report.errors, report.calls, report.input_tokens, report.pii_sweep_hits
```

or `schemaguru draft "shop.*" --provider anthropic`. Options:

- `budget=` caps the run (a `Budget`, or an int number of tokens); default `Budget.draft_budget_tokens` (unlimited).
- `verify=True` (default) checks the checkable parts right after drafting.

## Providers

| Provider | Extra | Notes |
|---|---|---|
| `AnthropicProvider(model="claude-sonnet-5-5", max_tokens=16000, force_tool=False)` | `anthropic` | structured output through a single `emit_draft` tool. By default `tool_choice` is `auto` plus an instruction to answer only through the tool, because the Anthropic API reference states that Sonnet 5.5 and Opus 5.5 reject a forced tool choice; this has **not** been verified with a live call. `force_tool=True` sends the forced form for models that accept it. |
| `OpenAICompatibleProvider(base_url, api_key, model)` | `openai` | any OpenAI-compatible endpoint, including local servers; uses `response_format` JSON schema |
| `CallableProvider(fn)` | none | wraps any function `fn(system, user, schema, max_tokens)` returning a dict, JSON text, or `(reply, Usage)`; `fn` may be async. Used by the tests. |

The default model for the CLI is `Config.llm_model` (`claude-sonnet-5-5`). Writing your own
provider: [[Extending-LLMProvider]].

## What the LLM is sent

Per table: names, types, type families, comments, nullability, catalog row count, measured null
rates and distinct counts, the declared primary and foreign keys, neighbouring table names, and
(only for low-cardinality columns) the top observed values **after the value policy**
([[PII-and-Sampling-Policy]]). Never random rows, never whole rows.

Real payload for `shop.products` (generated 2026-11-24 with a `CallableProvider` that records its
input; masked mode):

```json
{"tables":[{"columns":[{"comment":null,"distinct":200,"family":"integer","name":"product_id","null_rate":0.0,"nullable":false,"type":"INTEGER"},{"comment":null,"distinct":200,"family":"string","name":"name","null_rate":0.0,"nullable":true,"type":"VARCHAR"}],"comment":null,"foreign_keys":[],"kind":"table","neighbours":["shop.order_items"],"primary_key":["product_id"],"row_count":200,"table":"shop.products"}]}
```

Tables with at most 5 columns are batched, up to 8 per call; larger tables get one call each.
In `masked` mode a final shape sweep runs over every string of the payload before it leaves, and
every hit is counted in `report.pii_sweep_hits`. The prompt lives in
[`draft/prompts/table_v1.md`](https://github.com/rupakc/schemaguru/blob/main/src/schemaguru/draft/prompts/table_v1.md);
its hash is recorded on every drafted fact (`value["prompt_hash"]`).

## What comes back

The reply must match a JSON schema (`schemaguru.draft.DRAFT_SCHEMA`): per table `description`,
column `descriptions`, `use_cases` (at most 5), `entity_type` (entity + key columns),
`pitfalls` (rule, fix, severity), `soft_delete` (predicate + marker columns), `type_notes`, `pii`
guesses, `join_candidates` and `glossary` terms. The schema is validated client-side.

- A reply that fails the schema, or names a table or column that does not exist, is **retried
  once** for the failed tables only. A second failure records that table's draft as one `ERROR`
  fact (`SG401`) and applies none of it. Nothing is ever partially applied.
- A provider exception is `SG403` for that call's tables.
- Each draft fact gets `provenance=DRAFTED`, `status=UNVERIFIED`,
  `source="llm:<provider>:<model>"`.

## Verification of drafts

Every checkable part becomes a FactLang claim (within budget):

| Drafted part | Claim | Result |
|---|---|---|
| `soft_delete` | `ROWS t WHERE <p> VALUE >=1` and `ROWS t WHERE NOT (<p>) VALUE >=1` | both pass → `VERIFIED`; no live rows → `CONTRADICTED` |
| `type_notes` with a pattern | `PATTERN t.c LIKE '<pattern>' RATE >=99%` | pass → `VERIFIED` |
| `entity_type` with a key | `UNIQUE t.c [WITH [...]]` | pass → `VERIFIED` (the *key*; the entity name stays the model's words) |
| `join_candidates` | an exact overlap measurement | `OK` → `VERIFIED`, `BROKEN` → `CONTRADICTED`, `WEAK` stays `UNVERIFIED` with numbers |

A drafted soft-delete predicate is LLM text pasted into a claim, so it is first checked as a
filter expression only: no comment or statement markers, no FactLang keywords, balanced quotes
and parentheses, and it must reference one of its marker columns. A rejected predicate is never
sent (`SG406`). A claim the backend cannot judge leaves the draft `UNVERIFIED` with `SG405`; with
FactLang before 0.1.0 that is what you will see for every checkable part.

Prose (descriptions, use cases) stays `UNVERIFIED` until a person accepts it.

## Reviewing and accepting

```python
guru.accept([fact_id, ...])                         # HUMAN facts, source "accepted:<draft id>"
guru.accept(ids, write_overlay="schemaguru/accepted.yaml")
```

or interactively with `schemaguru review` (y/n/e(dit)/s(kip)/q(uit) per draft), `schemaguru
accept <ids>`, or `schemaguru export-drafts schemaguru/drafts.yaml` to review drafts as a YAML file
and copy what you like into your own overlay. The accepted overlay and the drafts file are
*machine-owned*: each starts with a "generated by schemaguru ... do not hand-edit" header, and
SchemaGuru refuses to overwrite any file without its own header. It never edits your files.

## A worked example

Drafting `shop.products` with a `CallableProvider` that returns a fixed reply (generated
2026-11-24; the claim check reported `SG405` because the installed FactLang has no claim
capability yet). The view before accepting:

```text
[shop.products] One row per product in the catalogue. (draft)
  Answers (draft): best-selling products; products never ordered
  200 rows (measured 2026-11-24)
  Columns:
    product_id  INTEGER  PK (unique, measured 2026-11-24); ← shop.order_items.product_id (coverage 100%); distinct 200 (measured 2026-11-24)
    name        VARCHAR  distinct 200 (measured 2026-11-24); Display name of the product. (draft)
  Provenance: drafted text marked (draft) is unchecked · measured 2026-11-24
```

After `guru.accept(...)` of the table description only:

```text
[shop.products] One row per product in the catalogue.
  Answers (draft): best-selling products; products never ordered
  200 rows (measured 2026-11-24)
  Columns:
    product_id  INTEGER  PK (unique, measured 2026-11-24); ← shop.order_items.product_id (coverage 100%); distinct 200 (measured 2026-11-24)
    name        VARCHAR  distinct 200 (measured 2026-11-24); Display name of the product. (draft)
  Provenance: drafted text marked (draft) is unchecked · human overlay · measured 2026-11-24
```

The accepted description lost its "(draft)" marker; the use cases and the column description,
not accepted, keep theirs.

## Guarantees

- Drafting never runs on the agent query path, from scoping or from rendering.
- Drafts never overwrite human facts: provenance is part of a fact's id, so both are kept and
  precedence picks the human one.
- Token usage per call is written to the ledger (`draft`, tokens). Each call reserves its estimated
  input; a call that could not write at least 1,024 output tokens within the remaining budget is
  not made (`SG402`). A provider that reports no usage is charged an estimate of about 4
  characters per token.
- Without any LLM, everything else in SchemaGuru works.

## See also

- [[PII-and-Sampling-Policy]] - what the LLM may see
- [[Overlays]] - writing meaning by hand instead
- [[Extending-LLMProvider]] - another model or endpoint
- [[Concepts]] - provenance and precedence
