# Server mode

The library needs no service. When a team wants one shared knowledge store behind HTTP —
several agents, several replicas — `schemaguru serve` (extra `server`: Starlette and uvicorn)
exposes the same operations as the Python API. The server is a thin layer: every route calls a
public `Guru` method or `handle_tool_call`, so there is no logic that only the server has.

```bash
pip install 'schemaguru[server,postgres-store]'
export SCHEMAGURU_SERVER_TOKENS="$ADMIN_TOKEN:admin,$READER_TOKEN:reader"
schemaguru --store postgresql://sg@db/knowledge --url "$WAREHOUSE_URL" serve --port 8080
```

## Routes

| Route | Calls | Role |
|---|---|---|
| `GET /healthz` | liveness; the only route without a token | — |
| `POST /context` | `Guru.context` (JSON `question`, `budget_tokens`, `profile`, `format`) | reader |
| `GET /search?q=&k=` | `search_tables` | reader |
| `GET /tables/{ref}` | `describe_table` | reader |
| `GET /paths?from=&to=` | `find_join_path` | reader |
| `GET /facts?subject=&kind=` | stored facts of one subject, masked by the tool surface's value policy | reader |
| `POST /refresh` | `Guru.refresh` (JSON `label`), one at a time | admin |
| `/mcp` | MCP over streamable HTTP when the installed MCP support provides it; otherwise `501` (`SG812`) | reader |

Errors are JSON `{"error": {"code": "SG8xx", "message": …}}`: a missing or invalid token is
`401` (`SG805`), a reader on an admin route `403` (`SG806`), a bad request `400` (`SG807`), an
unknown table `404` (`SG808`), a refresh already running `409` (`SG809`), anything unexpected
`500` (`SG810`, details logged, never returned).

## Authentication

v1 uses bearer tokens mapped to a role, `reader` or `admin`: from `tokens=` or the
`SCHEMAGURU_SERVER_TOKENS` environment variable (`token:role,…`; a token without a role is a
reader). Tokens must be at least 16 characters, are compared in constant time and are never
logged or echoed. With no usable token the server refuses to start (`SG811`). It binds to
`127.0.0.1` unless you pass `--host`. Bring your own scheme with an
[`Authenticator`](../extending/authenticator.md).

```py
from schemaguru.server import BearerTokens, create_app, serve

app = create_app(guru, authenticator=BearerTokens({"a-long-reader-token": "reader"}))  # ASGI
serve(guru, host="0.0.0.0", port=8080, tokens={"a-long-admin-token-123": "admin"})
```

## Sharing a store

`--store postgres://…` (extra `postgres-store`) opens a `PostgresStore`: the same schema as
the SQLite store, with weighted `tsvector` full-text search. Several replicas can share it;
refresh holds a Postgres advisory lock (`pg_try_advisory_lock`), so only one replica
refreshes at a time and the others get `409`. With a SQLite store a process lock does the
same within one process.

Overlay rules (waypoints, chains, waivers) live on the `Guru` object; to serve them, build
the `Guru` in Python, load the overlays, and pass it to `create_app` or `serve`.
