# Extending: LLMProvider

> An `LLMProvider` connects drafting to a model: it takes a system prompt, a user message and a JSON schema, and returns a dict that matches the schema.
> Shipped: `AnthropicProvider`, `OpenAICompatibleProvider` (any OpenAI-compatible endpoint, including local servers) and `CallableProvider` (wrap a function).
> For most custom backends `CallableProvider` is enough; implement the protocol when you want your own retries, usage reporting or async client.

## The protocol

```python
@runtime_checkable
class LLMProvider(Protocol):
    name: str
    def complete_json(self, system: str, user: str, schema: dict, *, max_tokens: int) -> dict: ...
    async def acomplete_json(self, system: str, user: str, schema: dict, *, max_tokens: int) -> dict: ...
```

Optional: `model: str` (recorded in `source="llm:<name>:<model>"`) and `last_usage: Usage(input_tokens,
output_tokens)` after each call (otherwise the pipeline estimates about 4 characters per token).
Raise `schemaguru.draft.InvalidReply` for a reply that is not a JSON object; the pipeline retries
once. Do not validate against the schema yourself unless you want to: the pipeline validates
every reply.

## Worked example

```python
class EchoProvider:
    """An LLMProvider skeleton: send (system, user, schema) somewhere, return a dict."""

    name = "echo"
    model = "echo-1"

    def __init__(self, call):
        self.call = call             # e.g. a function that posts to your model endpoint
        self.last_usage = None

    def complete_json(self, system, user, schema, *, max_tokens):
        return self.call(system, user, schema, max_tokens)    # must return a dict

    async def acomplete_json(self, system, user, schema, *, max_tokens):
        return self.complete_json(system, user, schema, max_tokens=max_tokens)
```

`isinstance(EchoProvider(fn), LLMProvider)` is `True` (checked against SchemaGuru 0.1.0). The
simplest route is usually:

```python
from schemaguru.draft import CallableProvider
provider = CallableProvider(lambda system, user, schema, max_tokens: my_client(system, user, schema))
guru.draft("shop.*", provider=provider)
```

The drafting page shows a full run with a `CallableProvider`, including the exact payload the
model receives ([[Drafting]]).

## Test template

```python
from schemaguru.draft import CallableProvider

def test_draft_with_scripted_provider(guru):
    reply = {"tables": [{"table": "shop.products", "description": "One row per product.",
                         "columns": [], "use_cases": [], "entity_type": None, "pitfalls": [],
                         "soft_delete": None, "type_notes": [], "pii": [],
                         "join_candidates": [], "glossary": []}]}
    report = guru.draft("shop.products", provider=CallableProvider(lambda *a: reply), verify=False)
    assert [f.kind.name for f in report.drafted] == ["DESCRIPTION"]

def test_negative_control_bad_reply_is_error(guru):
    report = guru.draft("shop.products", provider=CallableProvider(lambda *a: {"tables": [{}]}))
    assert any(d.code == "SG401" for d in report.diagnostics)   # report.errors holds the ERROR fact
```

Use scripted providers in tests, never recorded cassettes.

## See also

- [[Drafting]]
- [[PII-and-Sampling-Policy]]
- [[Configuration]]
- [[Security]]
