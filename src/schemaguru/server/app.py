"""The HTTP application (spec 17): a thin Starlette layer over one :class:`~schemaguru.Guru`.

Every route calls a public ``Guru`` method (or ``Guru.handle_tool_call``, whose payloads are
the agent surface's, already masked by the render layer) and adds no logic of its own. Routes:

=========================  ===============================================  ========
``GET  /healthz``          liveness; the only route without a token          —
``POST /context``          ``Guru.context`` (JSON ``question``, ``budget_tokens``,
                           ``profile``, ``format``)                          reader
``GET  /search``           ``search_tables`` (``q``, ``k``)                   reader
``GET  /tables/{ref}``     ``describe_table``                                 reader
``GET  /paths``            ``find_join_path`` (``from``, ``to``)              reader
``GET  /facts``            ``agent.facts.outbound_facts`` for one table or column
                           ``subject`` (``kind`` repeatable)                 reader
``POST /refresh``          ``Guru.refresh`` (JSON ``label``), one at a time   admin
``*    /mcp``              streamable-HTTP MCP when ``schemaguru.agent.mcp``
                           provides ``streamable_http_app(guru)``, else 501  reader
=========================  ===============================================  ========

Errors are JSON ``{"error": {"code": "SG8xx", "message": ...}}``. Refresh holds the store's
``advisory_lock("refresh")`` when it has one (``PostgresStore``: ``pg_try_advisory_lock``, so
replicas sharing the store never refresh concurrently) and a process lock otherwise; a refresh
that finds the lock taken answers 409 immediately.
"""

from __future__ import annotations

import importlib
import json
import logging
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from typing import TYPE_CHECKING, Any

from schemaguru._util.text import name_tokens
from schemaguru.agent.dispatch import error_text
from schemaguru.agent.facts import outbound_facts
from schemaguru.agent.tools import MAX_NAME, MAX_TEXT, tool_definitions
from schemaguru.catalog.executor import scrub_text
from schemaguru.errors import SchemaGuruError, StoreUnavailableError
from schemaguru.guru import Guru
from schemaguru.model.refs import ObjectRef
from schemaguru.server.auth import Authenticator

if TYPE_CHECKING:
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import Response

log = logging.getLogger("schemaguru.server")

MAX_BODY = 1 << 20  # bytes of a request body
MAX_TERMS = 64  # words in a question or search query
_CHALLENGE = {"WWW-Authenticate": 'Bearer realm="schemaguru"'}
_UNAVAILABLE = "the knowledge store is unavailable; retry later"


class _Fail(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


_STATUS = {"SG803": 400, "SG804": 500, "SG301": 429, "SG302": 429, "SG703": 503}


def _budget_range(guru: Guru) -> tuple[int, int]:
    """``schema_context``'s own ``budget_tokens`` bounds, so HTTP and the tool agree."""
    for spec in tool_definitions(guru):
        if spec["name"] == "schema_context":
            prop = spec["input_schema"]["properties"]["budget_tokens"]
            return int(prop.get("minimum", 0)), int(prop.get("maximum", 100_000))
    return 0, 100_000


class RefreshGuard:
    """One refresh at a time: the store's advisory lock if it has one, else a process lock."""

    def __init__(self, store: Any) -> None:
        self._store = store
        self._local = threading.Lock()

    @contextmanager
    def hold(self) -> Iterator[bool]:
        if not self._local.acquire(blocking=False):
            yield False
            return
        try:
            advisory: Callable[[str], AbstractContextManager[bool]] | None = getattr(
                self._store, "advisory_lock", None
            )
            if advisory is None:
                yield True
                return
            with advisory("refresh") as got:
                yield got
        finally:
            self._local.release()


def _mcp_app(guru: Guru) -> Any | None:
    try:
        module = importlib.import_module("schemaguru.agent.mcp")
    except ImportError:
        return None
    factory = getattr(module, "streamable_http_app", None)
    return factory(guru) if callable(factory) else None


def create_app(guru: Guru, *, authenticator: Authenticator) -> Starlette:
    """The Starlette application serving ``guru``; every route but ``/healthz`` needs a token."""
    try:
        from starlette.applications import Starlette
        from starlette.concurrency import run_in_threadpool
        from starlette.responses import JSONResponse
        from starlette.routing import Mount, Route
    except ImportError:
        raise ImportError(
            "server mode needs Starlette; install it with: pip install 'schemaguru[server]'"
        ) from None

    guard = RefreshGuard(guru.store)
    mcp_app = _mcp_app(guru)

    budget_min, budget_max = _budget_range(guru)

    def error(status: int, code: str, message: str) -> Response:
        """Every message leaves scrubbed (URL credentials) and masked like a tool's error."""
        try:
            text = error_text(guru, scrub_text(message))
        except Exception:
            text = "request failed"
        headers = _CHALLENGE if status == 401 else None
        body = {"error": {"code": code, "message": text}}
        return JSONResponse(body, status_code=status, headers=headers)

    def role_of(headers: Mapping[str, str]) -> str:
        role = authenticator.authenticate(headers)
        if role is None:
            raise _Fail(401, "SG805", "missing or invalid bearer token")
        return role

    async def read_body(request: Request) -> bytes:
        if request.method != "POST":
            return b""
        declared = request.headers.get("content-length")
        if declared is not None:
            if not declared.isdigit():
                raise _Fail(400, "SG807", "invalid Content-Length")
            if int(declared) > MAX_BODY:
                raise _Fail(413, "SG807", f"request body over {MAX_BODY} bytes")
        chunks: list[bytes] = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_BODY:
                raise _Fail(413, "SG807", f"request body over {MAX_BODY} bytes")
            chunks.append(chunk)
        return b"".join(chunks)

    def endpoint(need: str, fn: Callable[[Request, bytes], Any]) -> Callable[[Request], Any]:
        async def handle(request: Request) -> Response:
            try:
                role = role_of(request.headers)
                if need == "admin" and role != "admin":
                    raise _Fail(403, "SG806", "this route needs an admin token")
                body = await read_body(request)
                payload = await run_in_threadpool(fn, request, body)
                err = payload.get("error") if isinstance(payload, dict) else None
                if err is not None:  # a tool-level refusal: keep its (masked) code and message
                    code = str(err.get("code", "SG803")) if isinstance(err, dict) else "SG803"
                    msg = str(err.get("message", "")) if isinstance(err, dict) else str(err)
                    if code == "SG703":
                        msg = _UNAVAILABLE
                    raise _Fail(_STATUS.get(code, 400), code, msg)
                return JSONResponse(payload)
            except _Fail as f:
                return error(f.status, f.code, f.message)
            except StoreUnavailableError:
                log.warning("SG703 store unavailable on %s %s", request.method, request.url.path)
                return error(503, "SG703", _UNAVAILABLE)
            except SchemaGuruError as e:
                return error(400, "SG807", str(e))
            except Exception:
                log.exception("SG810 unhandled error on %s %s", request.method, request.url.path)
                return error(500, "SG810", "internal server error")

        return handle

    def json_body(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        try:
            data = json.loads(body)
        except ValueError:
            raise _Fail(400, "SG807", "the request body is not valid JSON") from None
        if not isinstance(data, dict):
            raise _Fail(400, "SG807", "the request body must be a JSON object")
        return data

    def need_param(request: Request, name: str, cap: int = MAX_NAME) -> str:
        value = request.query_params.get(name)
        if not value:
            raise _Fail(400, "SG807", f"missing query parameter {name!r}")
        if len(value) > cap:
            raise _Fail(400, "SG807", f"{name} is longer than {cap} characters")
        return value

    def text_arg(value: str, name: str) -> str:
        if len(value) > MAX_TEXT:
            raise _Fail(400, "SG807", f"{name} is longer than {MAX_TEXT} characters")
        if len(name_tokens(value)) > MAX_TERMS:
            raise _Fail(400, "SG807", f"{name} has more than {MAX_TERMS} words")
        return value

    def int_param(raw: Any, name: str, default: int) -> int:
        if raw is None:
            return default
        if isinstance(raw, bool):
            raise _Fail(400, "SG807", f"{name} must be an integer")
        try:
            return int(raw)
        except (TypeError, ValueError):
            raise _Fail(400, "SG807", f"{name} must be an integer") from None

    def not_found(e: SchemaGuruError) -> _Fail:
        if isinstance(e, StoreUnavailableError):
            raise e
        return _Fail(404, "SG808", str(e))

    # ----- routes (each runs in the thread pool; Guru is thread-safe) ------------------------

    def context(request: Request, body: bytes) -> dict[str, Any]:
        data = json_body(body)
        question = data.get("question")
        if not isinstance(question, str) or not question.strip():
            raise _Fail(400, "SG807", "'question' must be a non-empty string")
        text_arg(question, "question")
        budget = int_param(data.get("budget_tokens"), "budget_tokens", 4000)
        if not budget_min <= budget <= budget_max:
            raise _Fail(400, "SG807", f"budget_tokens must be in {budget_min}..{budget_max}")
        profile = str(data.get("profile", "agent"))
        fmt = str(data.get("format", "text"))
        try:
            view = guru.context(question, budget_tokens=budget, profile=profile, format=fmt)
        except (KeyError, ValueError) as e:
            raise _Fail(400, "SG807", str(e)) from None
        return {
            "text": str(view),
            "tables": [t.canonical() for t in view.tables],
            "fingerprint": view.fingerprint,
            "tokens": view.tokens,
            "format": fmt,
        }

    def search(request: Request, body: bytes) -> dict[str, Any]:
        k = int_param(request.query_params.get("k"), "k", 10)
        query = text_arg(need_param(request, "q", MAX_TEXT), "q")
        return guru.handle_tool_call("search_tables", {"query": query, "k": k})

    def table(request: Request, body: bytes) -> dict[str, Any]:
        name = str(request.path_params["ref"])
        if len(name) > MAX_NAME:
            raise _Fail(400, "SG807", f"the table name is longer than {MAX_NAME} characters")
        try:
            ref = guru.table_ref(name)
        except SchemaGuruError as e:
            raise not_found(e) from None
        return guru.handle_tool_call("describe_table", {"table": ref.canonical()})

    def paths(request: Request, body: bytes) -> dict[str, Any]:
        args = {"from": need_param(request, "from"), "to": need_param(request, "to")}
        return guru.handle_tool_call("find_join_path", args)

    def subject_of(text: str) -> ObjectRef:
        """A table or a column: the store's own resolution first (it keeps the column), then
        the façade's bare-name rules."""
        ref, cands = guru.store.resolve(text)
        if ref is not None:
            return ref
        if cands:
            listed = ", ".join(c.canonical() for c in cands[:20])
            raise _Fail(400, "SG807", f"{text!r} is ambiguous: {listed}")
        try:
            return guru.table_ref(text)
        except SchemaGuruError as e:
            if isinstance(e, StoreUnavailableError):
                raise
        try:
            return guru.column_ref(text)
        except SchemaGuruError as e:
            raise not_found(e) from None

    def facts(request: Request, body: bytes) -> dict[str, Any]:
        ref = subject_of(need_param(request, "subject"))
        kinds = request.query_params.getlist("kind") or None
        return outbound_facts(guru, ref, kinds)

    def refresh(request: Request, body: bytes) -> dict[str, Any]:
        label = json_body(body).get("label")
        if label is not None and not isinstance(label, str):
            raise _Fail(400, "SG807", "'label' must be a string")
        with guard.hold() as got:
            if not got:
                raise _Fail(409, "SG809", "a refresh is already running on this store")
            report = guru.refresh(label=label)
        data: dict[str, Any] = json.loads(report.to_json())
        return data

    async def healthz(request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    async def mcp_unavailable(request: Request) -> Response:
        try:
            role_of(request.headers)
        except _Fail as f:
            return error(f.status, f.code, f.message)
        return error(
            501,
            "SG812",
            "MCP over streamable HTTP is not available in this build: schemaguru.agent.mcp "
            "provides no streamable_http_app; use `schemaguru mcp` (stdio) instead",
        )

    routes: list[Any] = [
        Route("/healthz", healthz, methods=["GET"]),
        Route("/context", endpoint("reader", context), methods=["POST"]),
        Route("/search", endpoint("reader", search), methods=["GET"]),
        Route("/tables/{ref:path}", endpoint("reader", table), methods=["GET"]),
        Route("/paths", endpoint("reader", paths), methods=["GET"]),
        Route("/facts", endpoint("reader", facts), methods=["GET"]),
        Route("/refresh", endpoint("admin", refresh), methods=["POST"]),
    ]
    if mcp_app is None:
        routes.append(Route("/mcp", mcp_unavailable, methods=["GET", "POST", "DELETE"]))
    else:
        routes.append(Mount("/mcp", app=_Authed(mcp_app, authenticator)))
    app = Starlette(routes=routes)
    app.state.guru = guru
    app.state.refresh_guard = guard
    return app


class _Authed:
    """ASGI wrapper: the mounted MCP app is reachable only with a valid token."""

    def __init__(self, app: Any, authenticator: Authenticator) -> None:
        self._app, self._auth = app, authenticator

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
            if self._auth.authenticate(headers) is None:
                from starlette.responses import JSONResponse

                body = {"error": {"code": "SG805", "message": "missing or invalid bearer token"}}
                await JSONResponse(body, status_code=401, headers=_CHALLENGE)(scope, receive, send)
                return
        await self._app(scope, receive, send)
