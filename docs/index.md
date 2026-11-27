---
template: home.html
title: SchemaGuru
hide:
  - navigation
  - toc
---

<section class="sg-section sg-integrations" aria-labelledby="use-it-from" markdown>

## Use it from

<p class="sg-lead">One knowledge store, every surface: each calls the same <code>Guru</code>
methods and the same tools, so an agent sees the same view wherever it runs.</p>

<div class="sg-tabs" markdown>

<div role="tablist" aria-label="Ways to use SchemaGuru">
<button type="button" role="tab" id="sg-tab-python" aria-selected="true" aria-controls="sg-panel-python">Python</button>
<button type="button" role="tab" id="sg-tab-mcp" aria-selected="false" aria-controls="sg-panel-mcp" tabindex="-1">MCP</button>
<button type="button" role="tab" id="sg-tab-cli" aria-selected="false" aria-controls="sg-panel-cli" tabindex="-1">CLI</button>
<button type="button" role="tab" id="sg-tab-frameworks" aria-selected="false" aria-controls="sg-panel-frameworks" tabindex="-1">LangChain / LlamaIndex / OpenAI Agents</button>
<button type="button" role="tab" id="sg-tab-http" aria-selected="false" aria-controls="sg-panel-http" tabindex="-1">HTTP server</button>
</div>

<div id="sg-panel-python" role="tabpanel" aria-labelledby="sg-tab-python" tabindex="0" markdown>

```python
import schemaguru as sg

guru = sg.Guru.connect("duckdb:///warehouse.duckdb")  # crawls the catalog: metadata only
guru.measure(all=True)                                # what the data says: joins, values, nulls
guru.load_overlays("schemaguru.yaml")                 # what people know, in reviewable YAML
print(guru.context("monthly revenue by customer segment").text)

refused = guru.find_path("shop.orders", "shop.events")
print(refused.refusal, "-", refused.explanation)      # a route through a hub is refused
guru.close()
```

This block runs as written in the docs test, on the shop warehouse. More in
[Getting started](getting-started.md).

</div>

<div id="sg-panel-mcp" role="tabpanel" aria-labelledby="sg-tab-mcp" tabindex="0" markdown>

```bash
pip install 'schemaguru[mcp,duckdb]'
schemaguru --url duckdb:///warehouse.duckdb --store .schemaguru/shop.db mcp
```

Serves the tools, plus a `schema://tables/{ref}` resource per table, over stdio. Client
configuration: [Agents, tools and MCP](guide/agents.md#mcp).

</div>

<div id="sg-panel-cli" role="tabpanel" aria-labelledby="sg-tab-cli" tabindex="0" markdown>

```bash
pip install 'schemaguru[duckdb]'
export SCHEMAGURU_URL=duckdb:///warehouse.duckdb
schemaguru crawl
schemaguru measure --all
schemaguru context "monthly revenue by customer segment"
schemaguru path shop.orders shop.events    # exit 1: the route pivots through a hub
```

Every command: [CLI reference](reference/cli.md).

</div>

<div id="sg-panel-frameworks" role="tabpanel" aria-labelledby="sg-tab-frameworks" tabindex="0" markdown>

```py
from schemaguru.agent.adapters import langchain as sg_langchain

tools = sg_langchain.tools(guru)            # StructuredTools; results are JSON text
agent_model = chat_model.bind_tools(tools)
retriever = sg_langchain.retriever(guru, k=4)   # describe_table documents per hit
```

```py
from schemaguru.agent.adapters import llamaindex as sg_llamaindex

tools = sg_llamaindex.tool_spec(guru)       # FunctionTools
```

```py
from agents import Agent

from schemaguru.agent.adapters import openai_agents as sg_openai

agent = Agent(name="analyst", instructions=guru.prompt_block(),
              tools=sg_openai.function_tools(guru))
```

One extra each (`langchain`, `llamaindex`, `openai-agents`). See
[Framework adapters](guide/agents.md#framework-adapters).

</div>

<div id="sg-panel-http" role="tabpanel" aria-labelledby="sg-tab-http" tabindex="0" markdown>

```bash
pip install 'schemaguru[server,duckdb]'
export SCHEMAGURU_SERVER_TOKENS="$READER_TOKEN:reader"
schemaguru --url duckdb:///warehouse.duckdb --store .schemaguru/shop.db serve --port 8080

curl -s http://127.0.0.1:8080/context \
  -H "Authorization: Bearer $READER_TOKEN" -H "Content-Type: application/json" \
  -d '{"question": "monthly revenue by customer segment"}'
```

Routes, roles and a shared Postgres store: [Server mode](guide/server.md).

</div>

</div>

## Install

```bash
pip install schemaguru            # core: FactLang + PyYAML
pip install 'schemaguru[duckdb]'  # plus a warehouse driver (one extra per warehouse)
```

| Extra | Adds | Extra | Adds |
|---|---|---|---|
| `bigquery` | google-cloud-bigquery | `anthropic` | Anthropic drafting provider |
| `snowflake` | snowflake-connector-python | `openai` | OpenAI-compatible drafting provider |
| `redshift` | redshift-connector | `mcp` | `schemaguru mcp` (MCP SDK ≥ 2.3) |
| `databricks` | databricks-sql-connector | `server` | HTTP server (Starlette, uvicorn) |
| `postgres` | psycopg 3 | `langchain` | LangChain tools and retriever |
| `duckdb` | duckdb | `llamaindex` | LlamaIndex tools |
| `clickhouse` | clickhouse-connect | `openai-agents` | OpenAI Agents SDK tools |
| `mssql` | pyodbc (SQL Server, Synapse) | `embeddings` | numpy (embedding retriever) |
| `oracle` | oracledb | `tiktoken` | tiktoken token counter |
| `trino` | trino | `postgres-store` | Postgres knowledge store |
| `mysql` | PyMySQL (MySQL, MariaDB) | `all` | everything above |

The `lineage` extra (`pip install 'schemaguru[lineage]'`, sqlglot) reads lineage out of view
definitions and dbt SQL; see [Lineage](guide/lineage.md). SchemaGuru needs Python 3.10+.

</section>
