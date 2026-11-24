"""The MCP server (spec 18.3, extra ``mcp``): the agent tools plus ``schema://tables/{ref}``.

Built on the official ``mcp`` SDK's low-level ``Server`` (``mcp>=2``: handlers are passed to
the constructor as ``on_*`` callables) and served over stdio (``schemaguru mcp``) or, through
:func:`streamable_http_app`, over streamable HTTP (``schemaguru serve`` mounts it at ``/mcp``).
The SDK is imported lazily, so this module imports without the extra. Every tool call goes through
:func:`schemaguru.agent.dispatch.handle_tool_call` (value policy, never raises); a resource
read is ``describe_table`` of the ref.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import unquote

from schemaguru.__about__ import __version__
from schemaguru.agent.dispatch import MAX_ECHO, error_text, handle_tool_call
from schemaguru.agent.tools import ToolHost, tool_definitions

RESOURCE_PREFIX = "schema://tables/"
RESOURCE_TEMPLATE = RESOURCE_PREFIX + "{ref}"
MAX_LISTED_RESOURCES = 500  # tables listed by resources/list; any table is readable by template


def _sdk() -> tuple[Any, Any]:
    try:
        types = importlib.import_module("mcp.types")
        lowlevel = importlib.import_module("mcp.server.lowlevel")
    except ImportError as e:  # pragma: no cover - exercised only without the extra
        raise ImportError(
            "the MCP server needs the 'mcp' extra: pip install schemaguru[mcp]"
        ) from e
    return types, lowlevel


def resource_ref(uri: str) -> str | None:
    """The table ref of a ``schema://tables/{ref}`` URI (query and fragment dropped), or None."""
    if not uri.startswith(RESOURCE_PREFIX):
        return None
    ref = uri[len(RESOURCE_PREFIX) :].split("#", 1)[0].split("?", 1)[0]
    ref = unquote(ref).strip()
    return ref or None


def echo_uri(guru: ToolHost, uri: str) -> str:
    """What an error may say about a requested URI: its scheme plus the ref, cut to 200
    characters, quoted literals masked by the value policy (``?`` unless values are raw)."""
    scheme = uri.split("://", 1)[0] + "://" if "://" in uri else ""
    ref = resource_ref(uri) if uri.startswith(RESOURCE_PREFIX) else None
    if ref is None:
        return scheme
    shown = error_text(guru, "'" + ref.replace("'", "''")[:MAX_ECHO] + "'").strip("'")
    return RESOURCE_PREFIX + shown


def _not_found(uri: str, message: str, error: Any = None) -> Exception:
    """The SDK's resource-not-found error (INVALID_PARAMS with the URI, as ``MCPServer`` does)."""
    exceptions = importlib.import_module("mcp.shared.exceptions")
    jsonrpc = importlib.import_module("mcp_types.jsonrpc")
    data: dict[str, Any] = {"uri": uri}
    if error is not None:
        data["error"] = error
    err: Exception = exceptions.MCPError(code=jsonrpc.INVALID_PARAMS, message=message, data=data)
    return err


def build_server(guru: ToolHost, *, name: str = "schemaguru") -> Any:
    """An ``mcp.server.lowlevel.Server`` exposing the tools and table resources of ``guru``."""
    types, lowlevel = _sdk()

    async def list_tools(ctx: Any, params: Any) -> Any:
        tools = [
            types.Tool(name=s["name"], description=s["description"], input_schema=s["input_schema"])
            for s in tool_definitions(guru)
        ]
        return types.ListToolsResult(tools=tools)

    async def call_tool(ctx: Any, params: Any) -> Any:
        args = params.arguments if params.arguments is not None else {}
        result = await asyncio.to_thread(handle_tool_call, guru, params.name, args)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=result["text"])],
            structured_content=result,
            is_error="error" in result,
        )

    async def list_templates(ctx: Any, params: Any) -> Any:
        template = types.ResourceTemplate(
            name="table",
            uri_template=RESOURCE_TEMPLATE,
            description="Everything known about one table (the describe_table block).",
            mime_type="text/plain",
        )
        return types.ListResourceTemplatesResult(resource_templates=[template])

    async def list_resources(ctx: Any, params: Any) -> Any:
        def refs() -> list[str]:
            out: list[str] = []
            for t in guru.store.iter_tables():
                out.append(t.ref.canonical())
                if len(out) >= MAX_LISTED_RESOURCES:
                    break
            return out

        resources = [
            types.Resource(name=ref, uri=RESOURCE_PREFIX + ref, mime_type="text/plain")
            for ref in await asyncio.to_thread(refs)
        ]
        return types.ListResourcesResult(resources=resources)

    async def read_resource(ctx: Any, params: Any) -> Any:
        uri = str(params.uri)
        ref = resource_ref(uri)
        if ref is None:
            raise _not_found(
                echo_uri(guru, uri), f"SG803 unknown resource (expected {RESOURCE_TEMPLATE})"
            )
        result = await asyncio.to_thread(handle_tool_call, guru, "describe_table", {"table": ref})
        if "error" in result:
            raise _not_found(echo_uri(guru, uri), str(result["text"]), result["error"])
        text = str(result["text"])
        # the normalised URI: the resolved canonical ref, no query or fragment
        canonical = RESOURCE_PREFIX + str(result["table"])
        contents = types.TextResourceContents(uri=canonical, mime_type="text/plain", text=text)
        return types.ReadResourceResult(contents=[contents])

    return lowlevel.Server(
        name,
        version=__version__,
        instructions="Schema intelligence for one data warehouse: call schema_context first.",
        on_list_tools=list_tools,
        on_call_tool=call_tool,
        on_list_resource_templates=list_templates,
        on_list_resources=list_resources,
        on_read_resource=read_resource,
    )


async def serve_async(guru: ToolHost, *, name: str = "schemaguru") -> None:
    """Serve ``guru`` over stdio until the client disconnects."""
    server = build_server(guru, name=name)
    stdio = importlib.import_module("mcp.server.stdio")
    async with stdio.stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


class StreamableHTTPApp:
    """An ASGI app serving the MCP server over streamable HTTP (the SDK's session manager).

    The host must run :meth:`lifespan` around serving (``create_app`` puts it in the
    Starlette lifespan): the SDK's session manager runs only inside it, and it can run once per
    manager, so each lifespan builds a fresh one. Responses are JSON (``json_response``), and
    sessions are stateful. Authentication is the host's: ``create_app`` wraps this app with the
    same bearer check as every other route.
    """

    def __init__(self, guru: ToolHost, *, name: str = "schemaguru") -> None:
        self._server = build_server(guru, name=name)
        importlib.import_module("mcp.server.streamable_http_manager")  # fail early without it
        self._manager: Any = None

    @contextlib.asynccontextmanager
    async def lifespan(self) -> AsyncIterator[None]:
        managers = importlib.import_module("mcp.server.streamable_http_manager")
        manager = managers.StreamableHTTPSessionManager(app=self._server, json_response=True)
        async with manager.run():
            self._manager = manager
            try:
                yield
            finally:
                self._manager = None

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        manager = self._manager
        if manager is None:  # served without its lifespan: refuse clearly, never crash
            from starlette.responses import JSONResponse

            body = {"error": {"code": "SG812", "message": "the MCP session manager is not running"}}
            await JSONResponse(body, status_code=503)(scope, receive, send)
            return
        await manager.handle_request(scope, receive, send)


def streamable_http_app(guru: ToolHost, *, name: str = "schemaguru") -> StreamableHTTPApp:
    """The MCP server of ``guru`` as an ASGI app for streamable HTTP (``ImportError`` without
    the ``mcp`` extra)."""
    _sdk()
    return StreamableHTTPApp(guru, name=name)


def serve(guru: ToolHost, *, name: str = "schemaguru") -> None:
    """Blocking stdio MCP server (``schemaguru mcp``)."""
    asyncio.run(serve_async(guru, name=name))


__all__ = [
    "RESOURCE_TEMPLATE",
    "StreamableHTTPApp",
    "build_server",
    "resource_ref",
    "serve",
    "serve_async",
    "streamable_http_app",
]
