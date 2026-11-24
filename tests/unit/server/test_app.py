"""Server mode (spec 17): routes over a real Guru, bearer auth, the refresh lock."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("starlette")
pytest.importorskip("httpx")

from starlette.testclient import TestClient

from schemaguru.errors import ConfigError
from schemaguru.guru import Guru
from schemaguru.server import BearerTokens, create_app, serve
from schemaguru.server.app import RefreshGuard

ADMIN = "admin-token-0123456789"
READER = "reader-token-0123456789"
H_ADMIN = {"Authorization": f"Bearer {ADMIN}"}
H_READER = {"Authorization": f"Bearer {READER}"}


@pytest.fixture
def guru(shop_store: Any) -> Iterator[Guru]:
    g = Guru(store=shop_store)
    yield g
    g.close()


@pytest.fixture
def client(guru: Guru) -> TestClient:
    app = create_app(guru, authenticator=BearerTokens({ADMIN: "admin", READER: "reader"}))
    return TestClient(app, raise_server_exceptions=False)


PROTECTED = [
    ("POST", "/context"),
    ("GET", "/search?q=orders"),
    ("GET", "/tables/shop.orders"),
    ("GET", "/paths?from=orders&to=customers"),
    ("GET", "/facts?subject=shop.orders"),
    ("POST", "/refresh"),
    ("GET", "/mcp"),
]


@pytest.mark.parametrize(("method", "path"), PROTECTED)
def test_unauthenticated_401(client: TestClient, method: str, path: str) -> None:
    wrong = {"Authorization": "Bearer wrong-token-0123456789"}
    for headers in ({}, wrong, {"Authorization": ADMIN}):
        r = client.request(method, path, headers=headers)
        assert r.status_code == 401, (path, headers.keys())
        assert r.json()["error"]["code"] == "SG805"
        assert ADMIN not in r.text


def test_healthz_needs_no_token(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_context_endpoint_matches_library(client: TestClient, guru: Guru) -> None:
    q = "which customers placed orders"
    r = client.post("/context", headers=H_READER, json={"question": q, "budget_tokens": 3000})
    assert r.status_code == 200, r.text
    view = guru.context(q, budget_tokens=3000)
    body = r.json()
    assert body["text"] == str(view)
    assert body["fingerprint"] == view.fingerprint
    assert body["tables"] == [t.canonical() for t in view.tables]
    md = client.post("/context", headers=H_READER, json={"question": q, "format": "markdown"})
    assert md.json()["text"] == str(guru.context(q, format="markdown"))


def test_context_bad_requests(client: TestClient) -> None:
    for payload in ({}, {"question": ""}, {"question": "x", "budget_tokens": "many"},
                    {"question": "x", "format": "nope"}):  # fmt: skip
        r = client.post("/context", headers=H_READER, json=payload)
        assert r.status_code == 400 and r.json()["error"]["code"] == "SG807", payload
    r = client.post("/context", headers=H_READER, content=b"not json")
    assert r.status_code == 400
    r = client.post("/context", headers=H_READER, content=b"[1]")
    assert r.status_code == 400


def test_read_routes_match_tools(client: TestClient, guru: Guru) -> None:
    r = client.get("/search?q=orders&k=3", headers=H_READER)
    assert r.json() == guru.handle_tool_call("search_tables", {"query": "orders", "k": 3})
    r = client.get("/tables/shop.orders", headers=H_READER)
    assert r.json() == guru.handle_tool_call("describe_table", {"table": "shop.orders"})
    r = client.get("/paths?from=shop.orders&to=shop.customers", headers=H_READER)
    assert r.json() == guru.handle_tool_call(
        "find_join_path", {"from": "shop.orders", "to": "shop.customers"}
    )
    assert client.get("/search?k=3", headers=H_READER).status_code == 400
    r = client.get("/search?q=orders&k=500", headers=H_READER)
    assert r.status_code == 400 and r.json()["error"]["code"] == "SG803"
    r = client.get("/tables/shop.nope", headers=H_READER)
    assert r.status_code == 404 and r.json()["error"]["code"] == "SG808"


def test_facts_route_sweeps_values(client: TestClient, guru: Guru) -> None:
    from schemaguru.model.facts import Fact, Provenance, Status
    from schemaguru.model.kinds import FactKind
    from schemaguru.model.refs import ObjectRef

    ref = ObjectRef(None, "shop", "orders")
    guru.store.put_facts(
        [
            Fact.make(
                FactKind.DESCRIPTION,
                ref,
                {"text": "ask ops@example.com"},
                Provenance.HUMAN,
                Status.OBSERVED,
            )
        ]
    )
    r = client.get("/facts?subject=shop.orders&kind=DESCRIPTION", headers=H_READER)
    assert r.status_code == 200, r.text
    texts = [f["value"]["text"] for f in r.json()["facts"]]
    assert texts == ["ask <email>"]
    assert "example.com" not in r.text
    assert client.get("/facts?subject=shop.zzz", headers=H_READER).status_code == 404
    assert client.get("/facts", headers=H_READER).status_code == 400


def test_refresh_needs_admin(client: TestClient) -> None:
    r = client.post("/refresh", headers=H_READER)
    assert r.status_code == 403 and r.json()["error"]["code"] == "SG806"


class _Report:
    def to_json(self) -> str:
        return json.dumps({"exit_code": 0})


def test_refresh_lock_exclusive(
    client: TestClient, guru: Guru, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str | None] = []

    def fake_refresh(*, label: str | None = None) -> _Report:
        calls.append(label)
        return _Report()

    monkeypatch.setattr(guru, "refresh", fake_refresh)
    guard: RefreshGuard = client.app.state.refresh_guard  # type: ignore[attr-defined]
    with guard.hold() as got:
        assert got
        r = client.post("/refresh", headers=H_ADMIN, json={"label": "x"})
        assert r.status_code == 409 and r.json()["error"]["code"] == "SG809"
    assert calls == []
    r = client.post("/refresh", headers=H_ADMIN, json={"label": "nightly"})
    assert r.status_code == 200 and r.json() == {"exit_code": 0}
    assert calls == ["nightly"]
    assert client.post("/refresh", headers=H_ADMIN, json={"label": 3}).status_code == 400


def test_refresh_guard_uses_store_advisory_lock() -> None:
    held = threading.Lock()

    class _Store:
        def __init__(self) -> None:
            self.names: list[str] = []

        def advisory_lock(self, name: str) -> Any:
            from contextlib import contextmanager

            @contextmanager
            def cm() -> Iterator[bool]:
                self.names.append(name)
                got = held.acquire(blocking=False)
                try:
                    yield got
                finally:
                    if got:
                        held.release()

            return cm()

    a, b = RefreshGuard(_Store()), RefreshGuard(_Store())  # two replicas, one store lock
    with a.hold() as got_a, b.hold() as got_b:
        assert got_a is True and got_b is False


def test_mcp_without_http_factory_is_501(client: TestClient) -> None:
    r = client.get("/mcp", headers=H_READER)
    assert r.status_code == 501 and r.json()["error"]["code"] == "SG812"


def test_internal_errors_are_generic(
    client: TestClient, guru: Guru, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("secret detail")

    monkeypatch.setattr(guru, "handle_tool_call", boom)
    r = client.get("/search?q=x", headers=H_READER)
    assert r.status_code == 500 and r.json()["error"]["code"] == "SG810"
    assert "secret" not in r.text


def test_bearer_tokens() -> None:
    auth = BearerTokens({ADMIN: "admin", READER: "reader"})
    assert auth.authenticate({"authorization": f"Bearer {ADMIN}"}) == "admin"
    assert auth.authenticate({"Authorization": f"bearer {READER}"}) == "reader"
    assert auth.authenticate({"authorization": f"Basic {ADMIN}"}) is None
    assert auth.authenticate({"authorization": "Bearer "}) is None
    assert auth.authenticate({}) is None
    assert ADMIN not in repr(auth)
    for bad in ({}, {"short": "admin"}, {ADMIN: "root"}):
        with pytest.raises(ConfigError, match="SG811"):
            BearerTokens(bad)
    env = BearerTokens.from_env(f"{ADMIN}:admin, {READER}")
    assert env.authenticate({"authorization": f"Bearer {READER}"}) == "reader"
    with pytest.raises(ConfigError, match="SG811"):
        BearerTokens.from_env("")


def test_serve_defaults(guru: Guru, monkeypatch: pytest.MonkeyPatch) -> None:
    uvicorn = pytest.importorskip("uvicorn")
    seen: dict[str, Any] = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(kw, app=app))
    monkeypatch.delenv("SCHEMAGURU_SERVER_TOKENS", raising=False)
    with pytest.raises(ConfigError, match="SG811"):
        serve(guru)
    monkeypatch.setenv("SCHEMAGURU_SERVER_TOKENS", f"{ADMIN}:admin")
    serve(guru, port=9999)
    assert seen["host"] == "127.0.0.1" and seen["port"] == 9999
    serve(guru, tokens={READER: "reader"})
    assert seen["port"] == 8080


def test_codes_registered_and_documented() -> None:
    from schemaguru.diagnostics import CODES

    doc = (Path(__file__).resolve().parents[3] / "docs/reference/diagnostics.md").read_text(
        encoding="utf-8"
    )
    for code in ("SG703", "SG805", "SG806", "SG807", "SG808", "SG809", "SG810", "SG811", "SG812"):
        assert code in CODES and f"| {code} |" in doc, code
