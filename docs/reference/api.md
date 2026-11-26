# Python API

The public API is what `import schemaguru as sg` exposes (spec §19). It is stable within a
0.x minor release. Everything below is rendered from the source docstrings.

```python
import schemaguru as sg

print(sorted(n for n in sg.__all__ if n[0].isupper()))
```

## The façade

::: schemaguru.Guru
    options:
      members:
        - connect
        - crawl
        - measure
        - measure_queue
        - refresh
        - draft
        - accept
        - verify
        - load_overlays
        - find_path
        - lineage
        - context
        - fingerprint_views
        - report_error
        - feedback_summary
        - export
        - tools
        - handle_tool_call
        - prompt_block
        - diagnostics
        - close

## Configuration

::: schemaguru.Config

::: schemaguru.Budget

::: schemaguru.SamplingConfig

::: schemaguru.GraphConfig

::: schemaguru.ScopeConfig

::: schemaguru.CrawlConfig

## Knowledge model

::: schemaguru.ObjectRef

::: schemaguru.JoinRef

::: schemaguru.Fact

::: schemaguru.FactKind

::: schemaguru.Provenance

::: schemaguru.Status

## Results

::: schemaguru.ContextView

::: schemaguru.PathResult

::: schemaguru.Refusal

::: schemaguru.RefreshReport

::: schemaguru.LineageResult

::: schemaguru.Diagnostic

::: schemaguru.SchemaGuruError

## Helpers

::: schemaguru.guard.check_sql

## Subpackages

The subpackages `sg.catalog`, `sg.measure`, `sg.draft`, `sg.render`, `sg.scope`, `sg.graph`,
`sg.pii`, `sg.overlays` and `sg.guard` hold the extension points and building blocks; see
[Extending](../extending/index.md). The agent surface is `schemaguru.agent`
(tools, dispatcher, prompt block), `schemaguru.agent.mcp` and `schemaguru.agent.adapters.*`;
see [Agents, tools and MCP](../guide/agents.md).
