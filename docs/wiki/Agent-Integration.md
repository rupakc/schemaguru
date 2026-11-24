# Agent Integration

> SchemaGuru reaches an agent four ways: as prompt text (`guru.context`, `guru.prompt_block`), as JSON-schema tools (`guru.tools`, `guru.handle_tool_call`), as an MCP server (`schemaguru mcp`), and through thin adapters for LangChain, LlamaIndex, the OpenAI Agents SDK and the Anthropic SDK.
> Every tool result is JSON with a `text` field, passes through the value policy, and never raises: a bad call comes back as an `SG8xx` error the agent can read.
> No tool writes knowledge or spends warehouse money; the one tool that would (`check_fact`) is off by default and, when you allow it, runs under the measurement budget.

## 1. Prompt text

The simplest integration: put the view in the prompt.

```python
system = guru.prompt_block()                     # how to use the tools + the legend
context = guru.context(user_question, budget_tokens=3000)
prompt = f"{system}\n\n{context.text}\n\nQuestion: {user_question}"
```

`guru.prompt_block(question)` embeds the question's view directly. Real `prompt_block()` output on
the shop fixture (2026-11-24; `check_fact` is not listed: it is off by default, and the installed
FactLang does not publish its agent tool yet):

```text
You can inspect the data warehouse with these tools:
- schema_context: The warehouse tables, columns, measured joins and hazards relevant to a question. Call this first, before writing SQL.
- search_tables: Ranked tables matching a text query, each with a one-line description.
- describe_table: Everything known about one table: columns, keys, joins, values, pitfalls.
- find_join_path: Measured join routes between two tables with join keys and a SQL snippet, or the reason there is none. Never invent a join this tool does not return.
- column_values: Observed and declared values of one column, as the value policy allows them.
- explain_term: What a business term or metric means and which tables it maps to.
How to use them:
- Call schema_context with the user's question before writing SQL; use only the tables, columns and joins it shows, and heed every ⚠ line.
- Use search_tables when a table you need is not in the view, then describe_table for its full block.
- Join only along routes find_join_path returns; a refusal means there is no safe route. Never guess a join key or a table name: a bare name that matches several tables comes back as an error listing the candidates.
- Use column_values before filtering on a coded column; values may be masked by policy.
- Use explain_term for business terms and metrics.

Legend:
  ⚠ a hazard: measured, declared by a person, or drafted (then marked (draft)).
  ≈ / ≥ estimated from a sample of a large table; never verified.
  (draft) written by a language model and not checked.
  measured <date> an observed number; verified <date> a claim that passed its check.
  (human, unchecked) / (declared, unchecked) stated by a person or a declaration; not yet checked against the data.
  Absence of a warning is not verification.
```

For prompt caching, `context.blocks` lists `ViewBlock`s in order with `stable=True` on the blocks
that are the same for every question (global pitfalls, never-query list, legend); put the cache
breakpoint after the last stable block.

## 2. Tools

```python
tools = guru.tools()            # [{"name", "description", "input_schema"}, ...]
result = guru.handle_tool_call("search_tables", {"query": "refunds"})
result["text"]                  # always present
```

| Tool | Arguments | Returns |
|---|---|---|
| `schema_context` | `question`, `budget_tokens` (200..100,000, default 4,000) | the scoped view ([[Scoping]]) |
| `search_tables` | `query`, `k` (1..100, default 10) | ranked tables with one-line descriptions |
| `describe_table` | `table` (`schema.table`, or a bare name when unambiguous) | the full table block |
| `find_join_path` | `from`, `to` | routes with join keys (and a SQL snippet when FactLang can render one), or the refusal |
| `column_values` | `column` (`table.column`) | observed and declared values, as the value policy allows |
| `check_fact` | FactLang's claim argument | a FactLang verdict; **spends**, see below |
| `explain_term` | `term` | glossary or metric definition and its tables |

Real results on the shop fixture (2026-11-24, stand-in measurement backend):

```json
{
 "tables": [
  {
   "description": "One row per order placed on the storefront.",
   "score": 0.8255468065136086,
   "table": "shop.orders"
  }
 ],
 "text": "shop.orders \u2014 One row per order placed on the storefront."
}
```

```json
{
 "column": "shop.orders.status",
 "entries": [
  {
   "case_sensitive": true,
   "method": null,
   "provenance": "HUMAN",
   "split": ":",
   "status": "UNVERIFIED",
   "values": [
    "'paid'",
    "'refunded'",
    "'cancelled'"
   ]
  },
  {
   "case_sensitive": true,
   "distinct": 4,
   "method": "exact",
   "non_null": 10000,
   "observed": [
    {
     "count": 5000,
     "value": "'paid'"
    },
    {
     "count": 2588,
     "value": "'refunded'"
    },
    {
     "count": 2000,
     "value": "'cancelled'"
    },
    {
     "count": 412,
     "value": "'PAID'"
    }
   ],
   "provenance": "MEASURED",
   "split": null,
   "status": "OBSERVED"
  }
 ],
 "mode": "masked",
 "text": "shop.orders.status\n  values: 'paid','refunded','cancelled' (+ observed 'PAID')\n  \u26a0 WAIVED until 2027-01-31 (legacy upper-case rows being migrated): status: declared values 'paid','refunded','cancelled' \u2014 observed also 'PAID' (412 rows)\n  \u26a0 status: case drift: 'paid'/'PAID' differ only in case \u2014 compare case-insensitively"
}
```

Note how `column_values` returns both the person's expectation (`HUMAN`, `UNVERIFIED`) and the
measurement (`MEASURED`, `OBSERVED`, `method: exact`) side by side, and how `mode` says which value
policy applied.

### Errors are results

```json
{
 "error": {
  "code": "SG803",
  "message": "describe_table: unknown table ? (crawl first, or qualify the name)"
 },
 "text": "SG803 describe_table: unknown table ? (crawl first, or qualify the name)"
}
```

Unknown tool, bad arguments, an unknown or ambiguous name (`candidates` listed, never a guess) or
a tool not offered: `SG803`. Anything unexpected: `SG804` with the exception's type and a masked
message. Quoted values in error messages are shown as `?` unless the value policy is `raw`
(the call above asked for table `nosuch`; the message says `?`), because an argument may hold
personal data. Your agent loop never has to catch an exception from SchemaGuru.

### `check_fact` spends money

`check_fact` delegates to FactLang's `factlang_check`, which runs a claim on the warehouse.
Because an agent's question should never spend money by itself, it is **off by default**
(`Config.agent_checks="deny"`): the tool is not offered and calls to it are refused. With
`Config(agent_checks="allow")` it is offered when the installed FactLang publishes the tool, and
each check is admitted through the Guru's one per-period budget guard, the same one the
background drainer uses (one byte total per `Budget.period_s`, one day by default; `max_probes`
and `max_seconds` count across calls), and ledgered as `agent_check`. Over budget it is refused
with `SG301`/`SG302`. Its evidence leaves as an allow-list: verdict, claim text with quoted
literals shown as `?`, the SQL template, bytes, tier and duration; raw values, samples and
parameters never do. All other tools only read the store.

### Every result is masked

Tool results are outbound payloads: values go through the value policy of the tables they touch
(the most restrictive one when several are involved), free text through name masking, and the
whole result through the shape sweep ([[PII-and-Sampling-Policy]]).

## 3. MCP server

```bash
pip install "schemaguru[mcp]"
schemaguru mcp --url duckdb:///warehouse.duckdb        # or: --store .schemaguru/default.db
```

A stdio MCP server (official `mcp>=2.3` SDK) exposing the tools above plus resources
`schema://tables/{ref}` (each one the `describe_table` block; `resources/list` lists up to 500
tables, any table is readable by the template). Its instructions say "call schema_context first".
Add it to an MCP client with the command `schemaguru` and arguments `["mcp", "--store",
"/path/to/store.db"]`. In Python: `schemaguru.agent.mcp.build_server(guru)` / `serve(guru)`.
The same server is reachable over streamable HTTP at the [[Server]]'s `/mcp` route (`schemaguru serve`, bearer token required); in Python, `schemaguru.agent.mcp.streamable_http_app(guru)` is the ASGI app (run its `lifespan()` around serving).

## 4. Framework adapters

Each adapter only maps `guru.tools()` onto the framework's tool type and calls
`handle_tool_call`; there is no other logic. Each needs its own extra and imports it lazily.

| Framework | Extra | Call |
|---|---|---|
| LangChain | `langchain` | `schemaguru.agent.adapters.langchain.tools(guru)` → `StructuredTool`s; `.retriever(guru, k=4)` → a `BaseRetriever` returning one `describe_table` document per hit |
| LlamaIndex | `llamaindex` | `schemaguru.agent.adapters.llamaindex.tool_spec(guru)` → `FunctionTool`s |
| OpenAI Agents SDK | `openai-agents` | `schemaguru.agent.adapters.openai_agents.function_tools(guru)` → `FunctionTool`s (non-strict schemas) |
| Anthropic SDK | `anthropic` | `schemaguru.agent.adapters.anthropic.tools(guru)` (plain dicts for `messages.create`), `run_tool_use(guru, block)` → a `tool_result` block, `runner_tools(guru)` → `BetaFunctionTool`s for `client.beta.messages.tool_runner` |

The LangChain, LlamaIndex and OpenAI tools return the whole result as JSON text; the Anthropic
helpers return the `text` field (with `is_error` set for errors).

A manual Anthropic tool loop, as a sketch (not run against the live API for this page):

```python
import anthropic
from schemaguru.agent.adapters import anthropic as sga

client = anthropic.Anthropic()
messages = [{"role": "user", "content": question}]
while True:
    reply = client.messages.create(
        model="claude-sonnet-5-5", max_tokens=4096,
        system=guru.prompt_block(), tools=sga.tools(guru), messages=messages,
    )
    if reply.stop_reason != "tool_use":
        break
    messages.append({"role": "assistant", "content": reply.content})
    messages.append({"role": "user", "content": [
        sga.run_tool_use(guru, block) for block in reply.content if block.type == "tool_use"
    ]})
```

## Advice for agent builders

- Call `schema_context` first; join only along `find_join_path` routes; treat a refusal as "there
  is no safe route" and ask the user.
- Run the agent's SQL yourself, under a read-only role, optionally behind
  `sg.guard.check_sql` ([[Feedback-and-Guard]]), and send failures to `guru.report_error`.
- Measure before you deploy (`guru.measure(all=True)` within a budget), or the agent will see
  "unmeasured" everywhere.
- Fingerprint your key questions' views in CI ([[Rendering]]).

## See also

- [[Rendering]] - what the views look like
- [[Configuration]] - `agent_checks`, budgets, `measure_on_demand`
- [[Security]] - running agent SQL safely
- [[Server]] - the same operations over HTTP
