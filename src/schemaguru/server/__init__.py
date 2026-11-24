"""Optional HTTP server mode (spec 17, extra ``server``): the same operations as the Python API
over HTTP, as a thin layer over :class:`~schemaguru.Guru`."""

from __future__ import annotations

from collections.abc import Mapping

from schemaguru.guru import Guru
from schemaguru.server.app import create_app
from schemaguru.server.auth import Authenticator, BearerTokens


def serve(
    guru: Guru,
    *,
    host: str = "127.0.0.1",
    port: int = 8080,
    tokens: Mapping[str, str] | None = None,
    authenticator: Authenticator | None = None,
) -> None:
    """Run the server with uvicorn until interrupted (binds to localhost by default).

    Auth is ``authenticator``, else ``BearerTokens(tokens)``, else tokens parsed from
    ``SCHEMAGURU_SERVER_TOKENS`` (``token:role,...``). With none configured it refuses to start.
    """
    if authenticator is None:
        authenticator = BearerTokens(tokens) if tokens is not None else BearerTokens.from_env()
    try:
        import uvicorn
    except ImportError:
        raise ImportError(
            "server mode needs uvicorn; install it with: pip install 'schemaguru[server]'"
        ) from None
    app = create_app(guru, authenticator=authenticator)
    uvicorn.run(app, host=host, port=port, log_level="info", server_header=False)


__all__ = ["Authenticator", "BearerTokens", "create_app", "serve"]
