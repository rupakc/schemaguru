# SchemaGuru as an MCP server

`schemaguru mcp` serves the agent tools (`schema_context`, `search_tables`, `describe_table`,
`find_join_path`, `column_values`, `explain_term`, and `check_fact` when FactLang offers it)
plus one resource per table, `schema://tables/{ref}`, over **stdio**. Any MCP client can use
it: desktop assistants, IDE agents, or your own client built on the `mcp` SDK.

## 1. Install and build the knowledge store

```bash
pip install 'schemaguru[mcp,duckdb]'

# index the catalog once (metadata only); the store is a single SQLite file
schemaguru --url duckdb:///warehouse.duckdb --store .schemaguru/shop.db crawl
# optional: measure (spends warehouse time within the default budget)
schemaguru --url duckdb:///warehouse.duckdb --store .schemaguru/shop.db measure --all
```

`duckdb:///warehouse.duckdb` is relative to the working directory; an absolute POSIX path takes
four slashes (`duckdb:////data/warehouse.duckdb`).

## 2. Point your client at it

Most clients take a JSON block naming the command to start. With `--store` alone the server
answers from the store and never touches the warehouse. `check_fact` (claims run on the
warehouse, under the measurement budget) additionally needs `--url` and
`agent_checks: allow` in the config; it is off by default.

```json
{
  "mcpServers": {
    "schemaguru": {
      "command": "schemaguru",
      "args": ["--store", "/absolute/path/.schemaguru/shop.db", "mcp"]
    }
  }
}
```

Use absolute paths: clients start servers from their own working directory.

## 3. Check it from Python (optional)

The server object is the official SDK's low-level `Server`; you can build it in process to
inspect what a client will see:

```py
import schemaguru as sg
from schemaguru.agent.mcp import build_server, serve

guru = sg.Guru(store=".schemaguru/shop.db")  # store only: no warehouse connection
server = build_server(guru)  # mcp.server.lowlevel.Server
serve(guru)  # blocking stdio loop, what `schemaguru mcp` runs
```

## What the agent gets

Every tool result is JSON with a `text` field the client shows to the model; errors are
results too (`SG803` unknown tool or bad arguments, `SG804` an unexpected failure with a
masked message), never a crashed server. Values pass the same masking policy as the views.

Overlays: `schemaguru mcp` serves what is in the store. HUMAN facts written by
`guru.load_overlays(...)` from Python are in the store; waypoint rules, chains and waivers
live on the `Guru` object that loaded them, so serve from Python
(`serve(guru)` after `guru.load_overlays(...)`) when the agent must see them.
