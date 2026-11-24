"""SchemaGuru's tools with the Anthropic SDK's tool use, offline.

    python examples/anthropic_tools.py [path/to/warehouse.duckdb]

Shows the ``tools=`` list you pass to ``client.messages.create`` and answers ``tool_use``
blocks with ``run_tool_use``. No network and no SDK are needed here: the ``tool_use`` block
below is the dict shape the API returns (an SDK object works the same way). With the SDK
installed (``pip install 'schemaguru[anthropic]'``) the loop is::

    reply = client.messages.create(model=..., max_tokens=..., system=guru.prompt_block(),
                                   tools=sg_anthropic.tools(guru), messages=messages)
    results = [sg_anthropic.run_tool_use(guru, b) for b in reply.content if b.type == "tool_use"]
    messages += [{"role": "assistant", "content": reply.content},
                 {"role": "user", "content": results}]

``sg_anthropic.runner_tools(guru)`` returns the same tools for the SDK's tool runner.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from _shop import HERE, measured, shop_db, url

import schemaguru as sg
from schemaguru.agent.adapters import anthropic as sg_anthropic


def main() -> None:
    db = shop_db()
    with tempfile.TemporaryDirectory() as tmp:
        guru = sg.Guru.connect(url(db), store=str(Path(tmp) / "shop.db"))
        try:
            measured(guru.measure(all=True))
            guru.load_overlays(HERE / "schemaguru.yaml")

            tools = sg_anthropic.tools(guru)
            print("tools=[")
            for t in tools:
                print(f"  {t['name']}: {sorted(t['input_schema']['properties'])}")
            print("]\n")

            print("system prompt fragment (first lines):")
            print("\n".join(guru.prompt_block().splitlines()[:3]), "\n")

            block = {  # what the model sends back
                "type": "tool_use",
                "id": "toolu_01",
                "name": "describe_table",
                "input": {"table": "orders"},
            }
            result = sg_anthropic.run_tool_use(guru, block)
            print("tool_result for", block["name"], "is_error =", result["is_error"])
            print(result["content"])

            bad = {"type": "tool_use", "id": "toolu_02", "name": "drop_table", "input": {}}
            print("\nunknown tool ->", sg_anthropic.run_tool_use(guru, bad)["content"])
        finally:
            guru.close()


if __name__ == "__main__":
    main()
