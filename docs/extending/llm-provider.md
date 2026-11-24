# LLM provider

Drafting calls an LLM through one small protocol, so any model, gateway or local server
fits.

## Protocol

```py
class LLMProvider(Protocol):
    name: str
    def complete_json(self, system: str, user: str, schema: dict, *, max_tokens: int) -> dict: ...
    async def acomplete_json(self, system: str, user: str, schema: dict, *, max_tokens: int) -> dict: ...
```

* Return a dict matching `schema` (the drafting JSON schema, `schemaguru.draft.DRAFT_SCHEMA`).
  Raise `schemaguru.draft.InvalidReply` for a reply that is not such an object: the pipeline
  retries once, then records an `ERROR` draft for those tables (`SG401`). Any other exception
  is a provider failure (`SG403`).
* Optionally expose `model: str` (recorded on drafted facts) and `last_usage: Usage`
  (input/output tokens of the last call); without usage, tokens are estimated from length.
* `user` is a JSON document already masked by the value policy; send it as given.

Shipped: `AnthropicProvider`, `OpenAICompatibleProvider`, and `CallableProvider(fn)` for
wrapping a function (sync or async; it may return a dict, JSON text, or `(reply, Usage)`).

## Worked example: a provider class

A provider for an in-house gateway; the network call is replaced by a stub so this runs
offline.

```python
import json

import schemaguru as sg
from schemaguru.draft import InvalidReply, Usage


class GatewayProvider:
    name = "gateway"
    model = "house-model-1"

    def __init__(self, post):
        self.post, self.last_usage = post, None

    def complete_json(self, system, user, schema, *, max_tokens):
        reply = self.post({"system": system, "user": user, "schema": schema,
                           "max_tokens": max_tokens})
        try:
            data = json.loads(reply["text"])
        except ValueError as e:
            raise InvalidReply(f"not JSON: {e}") from None
        self.last_usage = Usage(reply["in"], reply["out"])
        return data

    async def acomplete_json(self, system, user, schema, *, max_tokens):
        return self.complete_json(system, user, schema, max_tokens=max_tokens)


def fake_gateway(request):
    tables = json.loads(request["user"])["tables"]
    drafts = [{"table": t["table"], "description": "One row per item in a basket.",
               "columns": [], "use_cases": [], "entity_type": None, "pitfalls": [],
               "soft_delete": None, "type_notes": [], "pii": [], "join_candidates": [],
               "glossary": []} for t in tables]
    return {"text": json.dumps({"tables": drafts}), "in": 900, "out": 120}


provider = GatewayProvider(fake_gateway)
guru = sg.Guru.connect("duckdb:///warehouse.duckdb")
report = guru.draft("shop.order_items", provider=provider)
print(report.calls, report.input_tokens, report.output_tokens, report.errors)
print(guru.handle_tool_call("describe_table", {"table": "order_items"})["text"].splitlines()[0])
guru.close()
```

## Test template

Check your provider against the schema it is given, on a scripted transport:

```py
from schemaguru.draft import DRAFT_SCHEMA, validate

def test_provider_returns_schema_valid_json():
    p = GatewayProvider(fake_gateway)
    user = json.dumps({"tables": [{"table": "shop.orders"}]})
    out = p.complete_json("system", user, DRAFT_SCHEMA, max_tokens=1000)
    assert validate(out, DRAFT_SCHEMA) == []      # the list of violations
    assert p.last_usage is not None
```
