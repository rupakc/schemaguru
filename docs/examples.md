# Examples

Runnable, offline examples live in the repository's
[`examples/`](https://github.com/rupakc/schemaguru/tree/main/examples) folder. Each builds the
shop warehouse on first run (from a repository checkout) or takes a DuckDB file as its first
argument; the docs test runs every one of them.

| Example | Shows | Needs |
|---|---|---|
| `duckdb_quickstart.py` | connect, view before and after measuring, an overlay, a safe and a refused join path | `schemaguru[duckdb]` |
| `langchain_agent.py` | the LangChain tools in a tool-calling loop driven by a scripted stand-in model, and the retriever | `schemaguru[duckdb,langchain]` |
| `anthropic_tools.py` | the `tools=` list for the Anthropic SDK and answering `tool_use` blocks with `run_tool_use` (no SDK, no network) | `schemaguru[duckdb]` |
| `large_table_sampling.py` | the sampling decision for a 2 TiB table, and how sampled numbers render (≈, intervals, lower bounds) | `schemaguru[duckdb]` |
| `mcp_server.md` | serving the tools over MCP and configuring a client | `schemaguru[mcp]` |
| `schemaguru.yaml` | the example overlay the others load | — |

```bash
git clone https://github.com/rupakc/schemaguru && cd schemaguru
pip install -e '.[duckdb]'
python examples/duckdb_quickstart.py
```

Until FactLang 0.1.0 ships its measurement capability, the examples print a note and show
catalog knowledge where they would show measured numbers; the docs test runs them with the
test suite's measurement backend instead.
