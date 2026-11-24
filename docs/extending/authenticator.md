# Server authenticator

[Server mode](../guide/server.md) asks an authenticator who is calling. The shipped one is
`BearerTokens`; replace it to plug in your identity provider, an API gateway's headers, or
mTLS information your proxy forwards.

## Protocol

```py
class Authenticator(Protocol):
    def authenticate(self, headers: Mapping[str, str]) -> str | None:
        """The caller's role ("reader" or "admin"), or None when not authenticated."""
```

`None` answers `401` (`SG805`); `"reader"` on an admin route answers `403` (`SG806`). Never
log or echo credentials, compare secrets in constant time, and fail closed: an exception or
an unknown header shape means `None`.

## Worked example: trusting a gateway's verified header

A gateway in front of the server verifies the user and forwards `X-Verified-Groups`; the
server trusts only requests that also carry the gateway's shared secret.

```python
import hmac

import schemaguru as sg
from schemaguru.server import create_app
from starlette.testclient import TestClient


class GatewayAuth:
    def __init__(self, secret: str) -> None:
        self._secret = secret.encode()

    def authenticate(self, headers):
        given = (headers.get("x-gateway-secret") or "").encode()
        if not hmac.compare_digest(given, self._secret):
            return None
        groups = {g.strip() for g in (headers.get("x-verified-groups") or "").split(",")}
        if "data-platform-admins" in groups:
            return "admin"
        return "reader" if "analysts" in groups else None


guru = sg.Guru.connect("duckdb:///warehouse.duckdb")
app = create_app(guru, authenticator=GatewayAuth("s3cret-from-the-vault"))
with TestClient(app) as client:
    ok = {"x-gateway-secret": "s3cret-from-the-vault", "x-verified-groups": "analysts"}
    print(client.get("/search", params={"q": "orders"}, headers=ok).json()["text"])
    print(client.get("/search", params={"q": "orders"}).status_code)
    print(client.post("/refresh", json={}, headers=ok).status_code)
guru.close()
```

## Test template

```py
def test_gateway_auth():
    auth = GatewayAuth("s3cret")
    assert auth.authenticate({}) is None
    assert auth.authenticate({"x-gateway-secret": "wrong", "x-verified-groups": "analysts"}) is None
    assert auth.authenticate({"x-gateway-secret": "s3cret", "x-verified-groups": "analysts"}) == "reader"
    assert auth.authenticate({"x-gateway-secret": "s3cret", "x-verified-groups": "x"}) is None
```

The shipped server tests (`tests/unit/server/test_app.py`) show how to drive every route with
Starlette's `TestClient`.
