# Agents, tools and MCP

SchemaGuru gives an agent knowledge three ways: a **view in the prompt**, **tools** it can
call, and an **MCP server** any MCP client can use. All three show the same knowledge
through the same value policy.

## Tools

`guru.tools()` returns JSON-schema tool specs (`name`, `description`, `input_schema`);
`guru.handle_tool_call(name, args)` runs one and always returns a JSON-safe dict with a
`text` field for the model — errors included, it never raises.

| Tool | Returns |
|---|---|
| `schema_context` | the question-scoped view ([Scoping](scoping.md)); call it first |
| `search_tables` | ranked tables with one-line descriptions |
| `describe_table` | everything known about one table |
| `find_join_path` | measured routes with join keys (and a SQL snippet when FactLang renders one), or the refusal |
| `column_values` | observed and declared values of a column, as the value policy allows |
| `check_fact` | runs a FactLang claim on the warehouse, under the measurement budget |
| `explain_term` | a glossary term or metric and the tables it maps to |

```python
import schemaguru as sg

guru = sg.Guru.connect("duckdb:///warehouse.duckdb")
guru.measure(all=True)
guru.load_overlays("schemaguru.yaml")

print([t["name"] for t in guru.tools()])
for name, args in [
    ("search_tables", {"query": "refunds"}),
    ("column_values", {"column": "orders.status"}),
    ("explain_term", {"term": "active customer"}),
    ("find_join_path", {"from": "orders", "to": "events"}),
    ("describe_table", {"table": "nope"}),
]:
    result = guru.handle_tool_call(name, args)
    print(f"--- {name}:", result.get("error", "ok"))
    print(result["text"])
```

* **`check_fact` spends, so it is off by default.** It is the one tool that runs on the
  warehouse. `Config(agent_checks="allow")` turns it on; every call is then priced and
  admitted through the `Guru`'s one per-period budget (shared with the background drainer,
  refilled per `Budget.period_s`) and recorded in the ledger. It also needs a FactLang that
  publishes its `factlang_check` tool definition. Its evidence reaches the agent as an
  allow-list only: verdict, claim text and SQL template with literals as `?`, bytes, duration.
* **Names are resolved, never guessed.** A bare name matching several tables returns an
  error listing the candidates.
* **Errors are results**: `SG803` for an unknown tool or invalid arguments, `SG804` for an
  unexpected failure (its type and a masked message).
* Every result passes the renderer's value policy, the outbound shape sweep and free-text
  masking ([PII](pii.md)).

## The prompt block

`guru.prompt_block()` is a system-prompt fragment: the tools, how to use them, the global
blocks and the legend. With a question it appends that question's view.

```text
--8<-- "generated/prompt_block.txt"
```

## Framework adapters

Each adapter only maps `guru.tools()` onto a framework's tool type (one extra each, imported
lazily); the dispatch is always `handle_tool_call`.

=== "Anthropic SDK"

    No SDK needed for the tool dicts or for answering `tool_use` blocks:

    ```python
    from schemaguru.agent.adapters import anthropic as sg_anthropic

    tools = sg_anthropic.tools(guru)                     # tools= for messages.create
    block = {"type": "tool_use", "id": "toolu_1", "name": "describe_table",
             "input": {"table": "payments"}}
    reply = sg_anthropic.run_tool_use(guru, block)       # a tool_result block
    print(reply["is_error"], reply["content"][:120])
    ```

    With the SDK (`pip install 'schemaguru[anthropic]'`):

    ```py
    import anthropic

    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": "Refund rate by month?"}]
    reply = client.messages.create(model="claude-sonnet-5-5", max_tokens=2048,
                                   system=guru.prompt_block(), tools=tools, messages=messages)
    results = [sg_anthropic.run_tool_use(guru, b) for b in reply.content if b.type == "tool_use"]
    # or let the SDK loop: client.beta.messages.tool_runner(..., tools=sg_anthropic.runner_tools(guru))
    ```

=== "LangChain"

    ```py
    from schemaguru.agent.adapters import langchain as sg_langchain

    tools = sg_langchain.tools(guru)            # StructuredTools; results are JSON text
    agent_model = chat_model.bind_tools(tools)
    retriever = sg_langchain.retriever(guru, k=4)   # describe_table documents per hit
    ```

=== "LlamaIndex"

    ```py
    from schemaguru.agent.adapters import llamaindex as sg_llamaindex

    tools = sg_llamaindex.tool_spec(guru)       # FunctionTools
    ```

=== "OpenAI Agents SDK"

    ```py
    from agents import Agent

    from schemaguru.agent.adapters import openai_agents as sg_openai

    agent = Agent(name="analyst", instructions=guru.prompt_block(),
                  tools=sg_openai.function_tools(guru))
    ```

Runnable, offline versions are in the repository: `examples/langchain_agent.py` (a scripted
model drives the tool loop) and `examples/anthropic_tools.py`.

## MCP

`schemaguru mcp` (extra `mcp`, MCP SDK ≥ 2.3) serves the tools plus a resource per table,
`schema://tables/{ref}` (the `describe_table` text), over stdio:

```bash
schemaguru --store .schemaguru/shop.db mcp                     # from the store only
schemaguru --url duckdb:///warehouse.duckdb --store .schemaguru/shop.db mcp   # warehouse attached
```

Client configuration and an in-process check: `examples/mcp_server.md`. From Python,
`schemaguru.agent.mcp.serve(guru)` serves a `Guru` you configured (with its overlays loaded).

```python
guru.close()
```

## Guarding the agent's SQL

SchemaGuru does not run agent SQL. If your host does, run it under a **read-only warehouse
role**; `sg.guard.check_sql` is a best-effort pre-check on top, not a sandbox — see
[Security](security.md#the-read-only-guard-helper).
