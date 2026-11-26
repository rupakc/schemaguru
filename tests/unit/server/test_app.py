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
    ("GET", "/lineage?ref=shop.orders"),
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


def test_mcp_without_the_extra_is_501(guru: Guru, monkeypatch: pytest.MonkeyPatch) -> None:
    import schemaguru.agent.mcp as mcp_mod

    def missing() -> Any:
        raise ImportError("no mcp")

    monkeypatch.setattr(mcp_mod, "_sdk", missing)
    app = create_app(guru, authenticator=BearerTokens({ADMIN: "admin", READER: "reader"}))
    with TestClient(app, raise_server_exceptions=False) as c:
        assert c.get("/mcp").status_code == 401  # still behind the token
        r = c.get("/mcp", headers=H_READER)
    assert r.status_code == 501 and r.json()["error"]["code"] == "SG812"
    assert "schemaguru[mcp]" in r.json()["error"]["message"]


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


# ----- review round 1: /facts through the agent outbound policy, masked errors, limits ---------

PLANTED = ("Bartholomew Kipling", "Quentin Shapeless", "wholesale_xq_secret", "Yolanda Brightwater",
           "kipling@example.com")  # fmt: skip


def g_now() -> Any:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


def _plant(guru: Guru) -> None:
    from schemaguru.model.facts import Fact, Provenance, Status
    from schemaguru.model.kinds import FactKind as F
    from schemaguru.model.refs import ObjectRef

    t = ObjectRef(None, "shop", "orders")
    email = ObjectRef(None, "shop", "customers", "email")

    def human(kind: Any, subject: ObjectRef, value: dict[str, Any]) -> Fact:
        return Fact.make(kind, subject, value, Provenance.HUMAN, Status.UNVERIFIED, source="o")

    guru.store.put_facts(
        [
            human(F.SOFT_DELETE, t, {"predicate": "note <> 'Quentin Shapeless'",
                                     "marker_columns": ["note"]}),
            human(F.PITFALL, t, {"rule": "rows where name = 'Yolanda Brightwater' are fake",
                                 "fix": None, "severity": "critical"}),
            Fact.make(F.FRESHNESS, t, {"column": "status", "max_value": "wholesale_xq_secret",
                                       "age_seconds": 10}, Provenance.MEASURED, Status.OBSERVED,
                      method="exact", observed_at=g_now()),
            human(F.PII, email, {"category": "email", "detector": "email"}),
            human(F.VALUES, email, {"values": ["Bartholomew Kipling", "kipling@example.com"],
                                    "case_sensitive": True, "split": None}),
            human(F.DESCRIPTION, email, {"text": "write to kipling@example.com"}),
        ]
    )  # fmt: skip


def _client_for(shop_store: Any, mode: str) -> tuple[Guru, TestClient]:
    from schemaguru.config import Config, SamplingConfig

    g = Guru(store=shop_store, config=Config(sampling=SamplingConfig(llm_values=mode)))  # type: ignore[arg-type]
    _plant(g)
    app = create_app(g, authenticator=BearerTokens({ADMIN: "admin", READER: "reader"}))
    return g, TestClient(app, raise_server_exceptions=False)


def _tool_views(g: Guru) -> str:
    """Everything the agent tools show about the planted tables (the policy /facts must match)."""
    parts = [
        g.handle_tool_call("describe_table", {"table": "shop.orders"}),
        g.handle_tool_call("describe_table", {"table": "shop.customers"}),
        g.handle_tool_call("column_values", {"column": "shop.customers.email"}),
    ]
    return json.dumps(parts)


@pytest.mark.parametrize("mode", ["masked", "none"])
def test_facts_planted_secrets_never_leave(shop_store: Any, mode: str) -> None:
    g, cl = _client_for(shop_store, mode)
    try:
        tools = _tool_views(g)
        # values on a PII column and e-mail shapes never leave; in none mode no value at all
        never = ["Bartholomew Kipling", "kipling@example.com"]
        if mode == "none":
            never += ["Quentin Shapeless", "wholesale_xq_secret", "Yolanda Brightwater"]
        for subject in ("shop.orders", "shop.customers.email", "customers.email"):
            r = cl.get("/facts", params={"subject": subject}, headers=H_READER)
            assert r.status_code == 200, (subject, r.text)
            assert not any(p in r.text for p in never), (mode, subject, r.text)
            # no drift: whatever /facts shows of a planted value, the tools show too
            assert all(p in tools for p in PLANTED if p in r.text), (mode, subject, r.text)
        col = cl.get("/facts", params={"subject": "shop.customers.email"}, headers=H_READER).json()
        assert col["subject"] == "shop.customers.email"  # the column is kept, not dropped
        assert {"VALUES", "PII", "DESCRIPTION"} <= {f["kind"] for f in col["facts"]}
        stored = {f.id for f in g.store.iter_facts()}
        assert all(f["id"] in stored for f in col["facts"])  # ids are not masked
        orders = cl.get("/facts", params={"subject": "shop.orders"}, headers=H_READER).json()
        sd = next(
            f["value"]["predicate"]
            for f in orders["facts"]
            if f["kind"] == "SOFT_DELETE" and "note" in f["value"]["predicate"]
        )
        assert sd == ("note <> ?" if mode == "none" else "note <> 'Quentin Shapeless'")
        # VALUES come out exactly as the column_values tool presents them
        tool = g.handle_tool_call("column_values", {"column": "shop.customers.email"})
        values = next(f for f in col["facts"] if f["kind"] == "VALUES")["value"]
        assert values == tool["entries"][0]
    finally:
        g.close()


@pytest.mark.parametrize("mode", ["masked", "none"])
def test_error_messages_are_masked_and_scrubbed(shop_store: Any, mode: str) -> None:
    g, cl = _client_for(shop_store, mode)
    try:
        r = cl.get("/tables/nope.Bartholomew Kipling", headers=H_READER)
        tool = g.handle_tool_call("describe_table", {"table": "nope.Bartholomew Kipling"})
        assert r.status_code == 404
        assert r.json()["error"]["message"] in tool["error"]["message"]  # masked like the tool
        if mode == "none":
            assert "Bartholomew" not in r.text
        r = cl.get("/facts", params={"subject": "postgres://u:s3cret-pw@db/x"}, headers=H_READER)
        assert r.status_code in (400, 404) and "s3cret" not in r.text
        r = cl.get("/tables/postgres://u:s3cret-pw@db/x", headers=H_READER)
        assert "s3cret" not in r.text
    finally:
        g.close()


def test_store_outage_is_503(
    client: TestClient, guru: Guru, monkeypatch: pytest.MonkeyPatch
) -> None:
    from schemaguru.errors import StoreUnavailableError

    def down(*a: Any, **k: Any) -> Any:
        raise StoreUnavailableError("SG703: cannot connect to the Postgres store at h:5432/db")

    monkeypatch.setattr(guru, "table_ref", down)
    r = client.get("/tables/shop.orders", headers=H_READER)
    assert r.status_code == 503 and r.json()["error"]["code"] == "SG703"
    assert "5432" not in r.text
    monkeypatch.setattr(guru.store, "search", down)
    r = client.get("/search?q=orders", headers=H_READER)
    assert r.status_code == 503 and r.json()["error"]["code"] == "SG703"


def test_401_carries_bearer_challenge(client: TestClient) -> None:
    r = client.get("/search?q=x")
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Bearer")
    assert "www-authenticate" not in client.get("/search?q=x", headers=H_READER).headers


def test_body_size_capped_before_and_while_reading(client: TestClient) -> None:
    from schemaguru.server.app import MAX_BODY

    r = client.post(
        "/context", headers={**H_READER, "content-length": str(MAX_BODY + 1)}, content=b"{}"
    )
    assert r.status_code == 413
    big = json.dumps({"question": "x" * (MAX_BODY + 10)}).encode()

    def chunks() -> Iterator[bytes]:  # no Content-Length: streamed
        for i in range(0, len(big), 65536):
            yield big[i : i + 65536]

    r = client.post("/context", headers=H_READER, content=chunks())
    assert r.status_code == 413 and r.json()["error"]["code"] == "SG807"


def test_request_caps_match_the_tools(client: TestClient) -> None:
    for budget in (199, 100_001, 10**9):
        r = client.post(
            "/context", headers=H_READER, json={"question": "orders", "budget_tokens": budget}
        )
        assert r.status_code == 400, budget
    many = " ".join(f"w{i}" for i in range(65))
    assert client.post("/context", headers=H_READER, json={"question": many}).status_code == 400
    assert client.get("/search", params={"q": many}, headers=H_READER).status_code == 400
    ok = " ".join(f"w{i}" for i in range(64))
    assert client.get("/search", params={"q": ok}, headers=H_READER).status_code == 200
    assert client.get("/tables/" + "x" * 600, headers=H_READER).status_code == 400


def test_store_credentials_rejected_is_500_config(
    client: TestClient, guru: Guru, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T21: rejected store credentials are a server configuration error (SG704, 500), never
    the SG703 'retry later' outage, and the response names no host, user or database."""
    from schemaguru.errors import StoreConfigError

    def refused(*a: Any, **k: Any) -> Any:
        raise StoreConfigError("SG704: the Postgres store at h:5432/db rejected user sg")

    monkeypatch.setattr(guru, "table_ref", refused)
    r = client.get("/tables/shop.orders", headers=H_READER)
    assert r.status_code == 500 and r.json()["error"]["code"] == "SG704"
    assert "5432" not in r.text and "h:" not in r.text
    monkeypatch.setattr(guru.store, "search", refused)
    r = client.get("/search?q=orders", headers=H_READER)
    assert r.status_code == 500 and r.json()["error"]["code"] == "SG704"


def _seed_lineage(guru: Guru) -> None:
    from schemaguru.model.facts import Fact, Provenance, Status
    from schemaguru.model.kinds import FactKind
    from schemaguru.model.lineage import table_lineage_value
    from schemaguru.model.refs import ObjectRef

    guru.store.put_facts(
        [
            Fact.make(
                FactKind.LINEAGE, ObjectRef(None, "shop", "payments"),
                table_lineage_value(["shop.orders"]), Provenance.CATALOG, Status.UNVERIFIED,
                source="view_definition",
            )
        ]
    )  # fmt: skip


def test_get_lineage(client: TestClient, guru: Guru) -> None:
    _seed_lineage(guru)
    r = client.get("/lineage", params={"ref": "payments"}, headers=H_READER)
    assert r.status_code == 200, r.text
    assert r.json() == guru.handle_tool_call("lineage", {"ref": "payments"})
    assert r.json()["edges"][0]["upstream"] == "shop.orders"
    params = {"ref": "shop.orders", "direction": "downstream", "depth": "3", "level": "table"}
    r = client.get("/lineage", params=params, headers=H_READER)
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["direction"], body["depth"]) == ("downstream", 3)
    assert [e["downstream"] for e in body["edges"]] == ["shop.payments"]
    r = client.get("/lineage", params={"ref": "shop.nowhere"}, headers=H_READER)
    assert r.status_code == 400 and r.json()["error"]["code"] == "SG803"


def test_get_lineage_bad_depth_400(client: TestClient) -> None:
    for depth in ("x", "0", "9"):
        r = client.get("/lineage", params={"ref": "orders", "depth": depth}, headers=H_READER)
        assert r.status_code == 400, (depth, r.text)
        assert r.json()["error"]["code"] in ("SG807", "SG803")
    r = client.get("/lineage", headers=H_READER)
    assert r.status_code == 400 and r.json()["error"]["code"] == "SG807"
    r = client.get("/lineage", params={"ref": "orders", "direction": "up"}, headers=H_READER)
    assert r.status_code == 400 and r.json()["error"]["code"] == "SG803"
