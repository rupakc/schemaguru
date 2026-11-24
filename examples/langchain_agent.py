"""SchemaGuru's tools in a LangChain tool-calling loop, with a scripted stand-in for the LLM.

    pip install 'schemaguru[langchain,duckdb]'
    python examples/langchain_agent.py [path/to/warehouse.duckdb]

No network: ``ScriptedModel`` plays the chat model and emits the tool calls a real model
would (schema_context first, then find_join_path), so you can see exactly what the agent gets
back. Swap it for any LangChain chat model with tool calling (``model.bind_tools(tools)``).
"""

# requires: langchain_core

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from _shop import HERE, measured, shop_db, url
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

import schemaguru as sg
from schemaguru.agent.adapters import langchain as sg_langchain


class ScriptedModel:
    """Plays a tool-calling chat model: a fixed script of AI messages, one per turn."""

    def __init__(self) -> None:
        self.script = [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "schema_context",
                        "args": {"question": "events per order", "budget_tokens": 1500},
                        "id": "call_1",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "find_join_path",
                        "args": {"from": "shop.orders", "to": "shop.events"},
                        "id": "call_2",
                    }
                ],
            ),
            AIMessage(
                content="There is no safe join from orders to events: every route pivots "
                "through the accounts hub and would multiply rows. Ask per account instead."
            ),
        ]

    def invoke(self, messages: list[BaseMessage]) -> AIMessage:
        return self.script.pop(0)


def main() -> None:
    db = shop_db()
    with tempfile.TemporaryDirectory() as tmp:
        guru = sg.Guru.connect(url(db), store=str(Path(tmp) / "shop.db"))
        try:
            measured(guru.measure(all=True))
            guru.load_overlays(HERE / "schemaguru.yaml")

            tools = {t.name: t for t in sg_langchain.tools(guru)}
            print("tools:", ", ".join(tools))

            model = ScriptedModel()
            messages: list[BaseMessage] = [HumanMessage("How many events does each order have?")]
            while True:
                reply = model.invoke(messages)
                messages.append(reply)
                if not reply.tool_calls:
                    print("\nanswer:", reply.content)
                    break
                for call in reply.tool_calls:
                    result = tools[call["name"]].invoke(call["args"])  # JSON text
                    print(f"\n--- {call['name']}({call['args']}) ->")
                    print(json.loads(result)["text"])
                    messages.append(ToolMessage(result, tool_call_id=call["id"]))

            # the retriever: one describe_table document per matching table
            docs = sg_langchain.retriever(guru, k=2).invoke("refunds")
            print("\nretriever:", [d.metadata["table"] for d in docs])
        finally:
            guru.close()


if __name__ == "__main__":
    main()
