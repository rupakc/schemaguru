# Extending: Authenticator

> An `Authenticator` decides who may call the HTTP server: it maps a request's headers to a role, `reader` or `admin`, or to `None` for "not authenticated".
> The shipped `BearerTokens` checks `Authorization: Bearer <token>` against a token map; write your own to use your identity provider, an API gateway header or mTLS details.
> Pass it as `create_app(guru, authenticator=...)` or `serve(guru, authenticator=...)`.

## The protocol

`schemaguru.server.Authenticator`:

```python
class Authenticator(Protocol):
    def authenticate(self, headers: Mapping[str, str]) -> str | None:
        """The caller's role, or None when the request is not authenticated."""
```

Roles are `"reader"` (every route except `/refresh`) and `"admin"` (all routes). `None` makes
the server answer 401 (`SG805`) with a `WWW-Authenticate: Bearer` challenge; a reader on an admin
route gets 403 (`SG806`). `/healthz` never asks. Header names arrive as the server received them;
look them up case-insensitively.

## Worked example: trust a gateway, keep a break-glass token

Behind an API gateway that has already authenticated the caller and sets `X-Auth-Groups`:

```python
from collections.abc import Mapping
from schemaguru.server import BearerTokens, serve


class GatewayGroups:
    """Role from the gateway's group header; falls back to bearer tokens."""

    def __init__(self, fallback: BearerTokens) -> None:
        self.fallback = fallback

    def authenticate(self, headers: Mapping[str, str]) -> str | None:
        groups = {g.strip() for g in (headers.get("x-auth-groups") or "").split(",")}
        if "schemaguru-admins" in groups:
            return "admin"
        if "analysts" in groups:
            return "reader"
        return self.fallback.authenticate(headers)


serve(guru, authenticator=GatewayGroups(BearerTokens({"<long-break-glass-token>": "admin"})))
```

Only trust such a header when the gateway strips it from incoming requests; otherwise anyone can
send it. Rules for any authenticator: never log or echo credentials, compare secrets in constant
time (`hmac.compare_digest`, as `BearerTokens` does over SHA-256 digests), and return `None`
rather than raising for bad input.

## Test template

```python
from starlette.testclient import TestClient
from schemaguru.server import BearerTokens, create_app

def test_roles(guru):
    app = create_app(guru, authenticator=GatewayGroups(BearerTokens({"t" * 20: "admin"})))
    c = TestClient(app)
    assert c.get("/search", params={"q": "orders"}).status_code == 401        # nobody
    assert c.get("/search", params={"q": "orders"},
                 headers={"X-Auth-Groups": "analysts"}).status_code == 200
    assert c.post("/refresh", json={}, headers={"X-Auth-Groups": "analysts"}).status_code == 403
```

There is no entry-point discovery yet (spec 21 plans `schemaguru.<point>` groups): pass the
object in.

## See also

- [[Server]]
- [[Security]]
- [[Agent-Integration]]
- [[Contributing]]
