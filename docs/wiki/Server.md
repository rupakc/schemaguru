# Server

> Server mode (`schemaguru serve`, extra `server`) is an optional HTTP service that exposes the same operations as the Python API, as a thin Starlette layer over one `Guru`.
> Every route except `/healthz` needs a bearer token with a role (`reader` or `admin`); several replicas can share one Postgres store, and only one refresh runs at a time across them.
> You do not need it: the library, the CLI and the stdio MCP server work without any server.

## Starting it

```bash
pip install "schemaguru[server,postgres-store]"
export SCHEMAGURU_SERVER_TOKENS="<long-admin-token>:admin,<long-reader-token>:reader"
schemaguru serve --store postgres://user@db-host/schemaguru --url "$SCHEMAGURU_URL" --port 8080
```

- `--host` defaults to `127.0.0.1` (localhost only); put a reverse proxy with TLS in front of it
  for remote access.
- `--store` may be a SQLite path or a `postgres://` / `postgresql://` DSN (the
  [[Postgres store|Store]]); `--url` is optional when the store is already filled.
- Tokens: `SCHEMAGURU_SERVER_TOKENS` holds `token:role` pairs separated by commas (a token without
  `:role` is a `reader`). Each token must be at least 16 characters. With no usable token the
  server refuses to start (real output):

```text
error: invalid configuration: SG811: no bearer tokens configured; set SCHEMAGURU_SERVER_TOKENS=<token>:admin,<token>:reader or pass tokens= / authenticator=
```

In Python:

```python
from schemaguru.server import BearerTokens, create_app, serve
serve(guru, host="127.0.0.1", port=8080, tokens={"<token>": "admin"})   # runs uvicorn
app = create_app(guru, authenticator=BearerTokens({"<token>": "reader"}))  # an ASGI app to mount yourself
```

## Routes

| Route | Does | Role |
|---|---|---|
| `GET /healthz` | liveness | none |
| `POST /context` | `Guru.context` (JSON `question`, `budget_tokens`, `profile`, `format`) | reader |
| `GET /search` | `search_tables` (`q`, `k`) | reader |
| `GET /tables/{ref}` | `describe_table` | reader |
| `GET /paths` | `find_join_path` (`from`, `to`) | reader |
| `GET /facts` | the facts of one table or column (`subject`, `kind` repeatable), through the agent value policy | reader |
| `POST /refresh` | `Guru.refresh` (JSON `label`); one at a time | admin |
| `/mcp` | MCP over streamable HTTP when `schemaguru.agent.mcp` provides it; **not in this build** (501) | reader |

Every route calls a public `Guru` method or `Guru.handle_tool_call`, so masking, budgets and
determinism are exactly the library's; no logic lives only in the server. Request bodies are
capped at 1 MiB, questions and queries at 20,000 characters and 64 words.

## Real responses

From the shop fixture behind `create_app` with Starlette's test client (generated 2026-11-24 on
`main`; measured numbers from the test suite's stand-in backend):

```text
>>> GET /healthz
HTTP 200
{
 "status": "ok"
}

>>> GET /search?q=refunds (no token)
HTTP 401
{
 "error": {
  "code": "SG805",
  "message": "missing or invalid bearer token"
 }
}

>>> GET /search?q=refunds
HTTP 200
{
 "tables": [
  {
   "description": "One row per order placed on the storefront.",
   "score": 0.8255468065136086,
   "table": "shop.orders"
  }
 ],
 "text": "shop.orders — One row per order placed on the storefront."
}

>>> GET /paths?from=shop.orders&to=shop.customers
HTTP 200
{
 "paths": [
  {
   "cost": 2.6180776587937986,
   "coverage": 0.9803921568627451,
   "provisional": false,
   "tables": [
    "shop.orders",
    "shop.customers"
   ]
  }
 ],
 "refusal": null,
 "text": "  shop.orders → shop.customers · support 98.04%\n    orders.customer_id = customers.customer_id (N:1)"
}

>>> GET /tables/shop.nosuch
HTTP 404
{
 "error": {
  "code": "SG808",
  "message": "unknown table ? (crawl first, or qualify the name)"
 }
}

>>> POST /refresh (reader token)
HTTP 403
{
 "error": {
  "code": "SG806",
  "message": "this route needs an admin token"
 }
}

>>> POST /mcp
HTTP 501
{
 "error": {
  "code": "SG812",
  "message": "MCP over streamable HTTP is not available in this build: schemaguru.agent.mcp provides no streamable_http_app; use `schemaguru mcp` (stdio) instead"
 }
}
```

The `/facts` route returns facts with the value policy applied (`mode`), and evidence as an
allow-list: verdict, claim text, the SQL template, bytes, duration; never raw values or
parameters. Abbreviated:

```text
>>> GET /facts?subject=shop.orders.status&kind=VALUES
HTTP 200
{
 "subject": "shop.orders.status",
 "mode": "masked",
 "facts": [
  {
   "id": "sha256:1fa4f55775453da42e86cb52db351ff621799614547d165c7b10ca99dad891c4",
   "kind": "VALUES",
   "provenance": "HUMAN",
   "status": "UNVERIFIED",
   "method": null,
   "observed_at": null,
   "snapshot_id": null,
   "source": "overlay:tests/fixtures/overlays/spec_example.yaml:15",
   "subject": "shop.orders.status",
   "value": {
    "provenance": "HUMAN",
    "status": "UNVERIFIED",
    "method": null,
    "case_sensitive": true,
    "split": ":",
    "values": [
     "'paid'",
     "'refunded'",
     "'cancelled'"
    ]
   }
  },
  {
   "id": "sha256:5e3e8776e19b8607ea83d438cdcfceb32ad3bca7fe3c23c62bb01da294a1e020",
   "kind": "VALUES",
   "provenance": "MEASURED",
   "status": "OBSERVED",
   "method": "exact",
   "observed_at": "2026-11-24T12:00:00+00:00",
   "snapshot_id": "live",
   "source": "measure",
   "subject": "shop.orders.status",
   "value": {
    "provenance": "MEASURED",
    "status": "OBSERVED",
    "method": "exact",
    "distinct": 4,
    "non_null": 10000,
    "case_sensitive": true,
    "split": null,
    "observed": [
     {
      "value": "'paid'",
      "count": 5000
     },
     {
      "value": "'refunded'",
      "count": 2588
     },
     {
      "value": "'cancelled'",
      "count": 2000
     },
     {
      "value": "'PAID'",
      "count": 412
     }
    ]
   },
   ...
```

## Errors and status codes

Errors are JSON `{"error": {"code": ..., "message": ...}}`; messages hide quoted values (`?`)
unless the value policy is `raw`.

| Status | Code | When |
|---|---|---|
| 400 | `SG807` (or `SG803`) | bad JSON, a missing or wrong-typed parameter, an ambiguous name |
| 401 | `SG805` | missing or invalid bearer token (with a `WWW-Authenticate` challenge) |
| 403 | `SG806` | the route needs an admin token |
| 404 | `SG808` | the table or column is not in the store |
| 409 | `SG809` | a refresh is already running on this store |
| 413 | `SG807` | request body over 1 MiB |
| 429 | `SG301` / `SG302` | a measurement budget refused the work |
| 500 | `SG810` | unexpected error; details are logged, never returned |
| 501 | `SG812` | MCP over HTTP is not available in this build |
| 503 | `SG703` | the shared store is unavailable (refused, lost, pool exhausted) |

## Replicas and refresh

Several server processes may share one [[Postgres store|Store]]. `POST /refresh` holds the store's
advisory lock (`pg_try_advisory_lock`); a refresh that finds it taken answers 409 at once, so
replicas never refresh concurrently. With a SQLite store a process lock is used instead. A common
setup: the replicas serve reads, and one scheduled job calls `POST /refresh` (admin token) or runs
`schemaguru refresh` against the same store.

## Authentication is pluggable

`BearerTokens` compares tokens in constant time (SHA-256 digests with `hmac.compare_digest`) and
never logs or echoes them. To use your own identity provider, implement an `Authenticator`
([[Extending-Authenticator]]).

## See also

- [[Store]] - the Postgres store the replicas share
- [[Agent-Integration]] - the same operations as tools and over stdio MCP
- [[Security]] - deployment advice
- [[CLI]] - `serve` and its options
