# Drafting with an LLM

Measurement says *what is there*; it cannot say *what it means*. Drafting asks an LLM for
the meaning — table and column descriptions, the questions a table answers, its entity
type, pitfalls, a soft-delete guess, type notes, PII guesses, join candidates and glossary
terms — and stores the answer as **drafts**: `DRAFTED` facts marked *(draft)* in every view
until a check passes or a person accepts them.

Everything else in SchemaGuru works with no LLM at all.

## A run

```python
import json

import schemaguru as sg
from schemaguru.draft import CallableProvider


def scripted_llm(system: str, user: str, schema: dict, max_tokens: int) -> dict:
    """Stands in for a model: answers every table it is shown (see the input it gets)."""
    tables = json.loads(user)["tables"]
    return {"tables": [{
        "table": t["table"],
        "description": f"One row per {t['table'].split('.')[-1].rstrip('s')}.",
        "columns": [], "use_cases": ["How many are there per month?"],
        "entity_type": None, "pitfalls": [], "soft_delete": None, "type_notes": [],
        "pii": [], "join_candidates": [], "glossary": [],
    } for t in tables]}


guru = sg.Guru.connect("duckdb:///warehouse.duckdb")
report = guru.draft(["shop.products", "shop.sessions"], provider=CallableProvider(scripted_llm))
print(report.calls, "call(s),", report.tokens, "tokens (estimated:", report.estimated_tokens, ")")
for f in report.drafted:
    print(f.kind.name, f.subject.canonical(), f.status.value, dict(f.value))
```

Real providers:

```py
from schemaguru.draft import AnthropicProvider, OpenAICompatibleProvider

guru.draft(["analytics.*"], provider=AnthropicProvider())        # model claude-sonnet-5-5
guru.draft(["analytics.*"], provider=OpenAICompatibleProvider(
    base_url="http://localhost:8000/v1", api_key=None, model="my-local-model"))
```

`AnthropicProvider` (extra `anthropic`) asks for structured output through a tool; forcing
the tool choice is **off by default** (`force_tool=True` turns it on), following the
Anthropic API reference for Sonnet 5.5 — this has not been verified live.
`OpenAICompatibleProvider` (extra `openai`) works with any OpenAI-compatible endpoint,
including local servers. `CallableProvider(fn)` wraps any function; `fn` may be async and may
return `(reply, Usage)` when it knows the token usage. Writing your own:
[LLM provider](../extending/llm-provider.md).

## What the model sees

One call per table (small tables are batched): table and column names, types, comments,
catalog statistics, the measured facts (null rates, distinct counts), declared constraints,
neighbouring table names — and observed values **only as the value policy allows**
(masked by default; see [PII](pii.md)). A final shape sweep runs over every outbound payload
in masked mode; hits are masked and counted (`report.pii_sweep_hits`).

The reply must match a JSON schema (`schemaguru.draft.DRAFT_SCHEMA`). An invalid reply is
retried once; after that the table's draft is an `ERROR` fact (`SG401`) — never partially
applied. A provider failure costs that call's tables (`SG403`). `draft_budget_tokens` (or
`budget=` on the call) caps a run (`SG402`). Prompts are versioned files in
`schemaguru/draft/prompts/`; their hash is recorded on every drafted fact.

## Checking drafts

Every checkable part of a draft becomes a FactLang claim and is run within budget: a
soft-delete predicate (must parse as a safe filter, else `SG406`, and must leave some rows
and exclude some), type notes (`PATTERN`), join candidates (`JOIN`), an entity type
(`UNIQUE` on the claimed key). Passing parts become `VERIFIED`; prose stays a draft. A claim
that cannot be checked (no verdict, e.g. FactLang lacks the capability) stays `UNVERIFIED`
(`SG405`).

## Accepting drafts

A person turns drafts into `HUMAN` facts (source `accepted:<draft id>`); human facts always
win over drafts for meaning.

```python
ids = [f.id for f in report.drafted if f.kind.name == "DESCRIPTION"]
accepted = guru.accept(ids, write_overlay="schemaguru/accepted.yaml")
print(len(accepted), "accepted;", open("schemaguru/accepted.yaml", encoding="utf-8").read()[:120])
guru.close()
```

From the terminal: `schemaguru draft 'shop.*'`, then `schemaguru review` (walks the drafts:
yes / no / edit / skip / quit) or `schemaguru accept <ids…>`; `schemaguru export-drafts`
writes them to a separate, reviewable YAML file. SchemaGuru never edits a file you wrote:
`--write-overlay` appends to a machine-owned `accepted.yaml`.

Drafting never runs on an agent's query path; it runs only when you call it.
