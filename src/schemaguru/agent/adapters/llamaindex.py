"""LlamaIndex adapter (spec 18.4, extra ``llamaindex``): ``FunctionTool``s."""

from __future__ import annotations

import copy
from typing import Any

from schemaguru.agent.adapters import bind, need
from schemaguru.agent.tools import ToolHost, tool_definitions


def _schema_model(name: str, schema: dict[str, Any]) -> Any:
    """A pydantic model whose JSON schema is exactly the tool's (``fn_schema`` must be a model)."""
    pydantic = need("pydantic", "llamaindex")
    frozen = copy.deepcopy(schema)

    def model_json_schema(cls: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return copy.deepcopy(frozen)

    return type(
        f"{name}_args",
        (pydantic.BaseModel,),
        {
            "model_config": pydantic.ConfigDict(extra="allow"),
            "model_json_schema": classmethod(model_json_schema),
        },
    )


def tool_spec(guru: ToolHost) -> list[Any]:
    """One ``FunctionTool`` per agent tool; its output is the result as JSON text."""
    lt = need("llama_index.core.tools", "llamaindex")
    out: list[Any] = []
    for spec in tool_definitions(guru):
        out.append(
            lt.FunctionTool.from_defaults(
                fn=bind(guru, spec["name"]),
                name=spec["name"],
                description=spec["description"],
                fn_schema=_schema_model(spec["name"], spec["input_schema"]),
            )
        )
    return out


__all__ = ["tool_spec"]
