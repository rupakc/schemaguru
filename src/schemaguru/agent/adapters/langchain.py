"""LangChain adapter (spec 18.4, extra ``langchain``): ``StructuredTool``s and a retriever."""

from __future__ import annotations

from typing import Any

from schemaguru.agent.adapters import bind, need
from schemaguru.agent.dispatch import handle_tool_call
from schemaguru.agent.tools import ToolHost, tool_definitions


def tools(guru: ToolHost) -> list[Any]:
    """One ``StructuredTool`` per agent tool; ``args_schema`` is the tool's JSON schema and the
    tool returns the result as JSON text."""
    lc = need("langchain_core.tools", "langchain")
    out: list[Any] = []
    for spec in tool_definitions(guru):
        out.append(
            lc.StructuredTool.from_function(
                func=bind(guru, spec["name"]),
                name=spec["name"],
                description=spec["description"],
                args_schema=spec["input_schema"],
                infer_schema=False,
            )
        )
    return out


def retriever(guru: ToolHost, *, k: int = 4) -> Any:
    """A ``BaseRetriever``: ``search_tables`` for the query, one ``describe_table`` document
    (metadata ``table``) per hit. ``k`` must be 1..100 (``ValueError`` otherwise)."""
    if isinstance(k, bool) or not isinstance(k, int) or not 1 <= k <= 100:
        raise ValueError(f"k must be an integer between 1 and 100, not {k!r}")
    retrievers = need("langchain_core.retrievers", "langchain")
    documents = need("langchain_core.documents", "langchain")

    class SchemaGuruRetriever(retrievers.BaseRetriever):  # type: ignore[misc,name-defined]
        host: Any
        k: int = 4

        def _get_relevant_documents(self, query: str, *, run_manager: Any = None) -> list[Any]:
            hits = handle_tool_call(self.host, "search_tables", {"query": query, "k": self.k})
            docs: list[Any] = []
            for row in hits.get("tables", []):
                block = handle_tool_call(self.host, "describe_table", {"table": row["table"]})
                if "error" not in block:
                    docs.append(
                        documents.Document(
                            page_content=block["text"], metadata={"table": row["table"]}
                        )
                    )
            return docs

    return SchemaGuruRetriever(host=guru, k=k)


__all__ = ["retriever", "tools"]
