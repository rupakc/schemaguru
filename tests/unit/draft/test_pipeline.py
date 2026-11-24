from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from schemaguru.config import Budget, Config, SamplingConfig
from schemaguru.draft import draft_tables
from schemaguru.draft.provider import DRAFT_SCHEMA, Usage, validate
from schemaguru.draft.providers import CallableProvider
from schemaguru.knowledge.resolve import resolve
from schemaguru.model.facts import Fact, Provenance, Status
from schemaguru.model.joins import JoinRef
from schemaguru.model.kinds import FactKind
from schemaguru.model.refs import ObjectRef
from schemaguru.store.sqlite import SQLiteStore

from .conftest import Script, table_reply

S = "shop"
CUST = ObjectRef(None, S, "customers")
ORD = ObjectRef(None, S, "orders")
ACC = ObjectRef(None, S, "accounts")
PROD = ObjectRef(None, S, "products")
AT = datetime(2026, 11, 20, tzinfo=timezone.utc)


def _emails(store: SQLiteStore) -> None:
    store.put_facts(
        [
            Fact.make(
                FactKind.VALUES,
                ObjectRef(None, S, "customers", "email"),
                {
                    "case_sensitive": True,
                    "split": None,
                    "observed": [
                        {"value": "ann.lee@example.com", "count": 2},
                        {"value": "bo@example.org", "count": 1},
                    ],
                    "distinct": 2,
                    "non_null": 3,
                },
                Provenance.MEASURED,
                Status.OBSERVED,
                method="exact",
                observed_at=AT,
            )
        ]
    )


def test_draft_payload_masked(shop_store: SQLiteStore) -> None:
    _emails(shop_store)
    script = Script()
    report = draft_tables([CUST], store=shop_store, provider=script.provider(), config=Config())
    assert len(script.calls) == 1
    user = script.calls[0]["user"]
    assert "ann.lee@example.com" not in user and "bo@example.org" not in user
    assert user.count("<email>") == 2
    assert report.errors == [] and report.drafted


def test_raw_mode_only_on_opt_in(shop_store: SQLiteStore) -> None:
    _emails(shop_store)
    default, opted = Script(), Script()
    draft_tables([CUST], store=shop_store, provider=default.provider(), config=Config())
    cfg = Config(sampling=SamplingConfig(llm_values="raw"))
    draft_tables([CUST], store=shop_store, provider=opted.provider(), config=cfg)
    assert "ann.lee@example.com" not in default.calls[0]["user"]
    assert "ann.lee@example.com" in opted.calls[0]["user"]


def test_none_mode_sends_no_values(shop_store: SQLiteStore) -> None:
    _emails(shop_store)
    script = Script()
    cfg = Config(sampling=SamplingConfig(llm_values="none"))
    draft_tables([CUST], store=shop_store, provider=script.provider(), config=cfg)
    user = script.calls[0]["user"]
    assert "<email>" not in user and "example.com" not in user
    email = next(
        c for c in script.calls[0]["payload"]["tables"][0]["columns"] if c["name"] == "email"
    )
    assert "values" not in email


def test_drafted_facts_shape(shop_store: SQLiteStore) -> None:
    def answer(tables: list[dict[str, Any]], n: int) -> dict[str, Any]:
        return {
            "tables": [
                table_reply(
                    "shop.orders",
                    columns=[{"name": "status", "description": "Order state."}],
                    entity_type={"entity": "order", "key": ["order_id"]},
                    pitfalls=[
                        {
                            "rule": "status is case-mixed",
                            "fix": "compare lower(status)",
                            "severity": "warn",
                        }
                    ],
                    soft_delete={
                        "predicate": "deleted IS NULL OR NOT deleted",
                        "marker_columns": ["deleted"],
                    },
                    type_notes=[
                        {
                            "column": "created_at",
                            "stored_as": "string",
                            "means": "ISO timestamp",
                            "pattern": "____-__-__%",
                        }
                    ],
                    pii=[{"column": "customer_id", "category": "other"}],
                    join_candidates=[
                        {
                            "columns": ["customer_id"],
                            "to_table": "shop.customers",
                            "to_columns": ["customer_id"],
                        }
                    ],
                    glossary=[{"term": "order", "definition": "A purchase."}],
                )
            ]
        }

    script = Script(answer)
    report = draft_tables([ORD], store=shop_store, provider=script.provider(), config=Config())
    kinds = sorted(f.kind.name for f in report.drafted)
    assert kinds == sorted(
        [
            "DESCRIPTION",
            "DESCRIPTION",
            "USE_CASES",
            "ENTITY_TYPE",
            "PITFALL",
            "SOFT_DELETE",
            "TYPE_NOTE",
            "PII",
            "JOIN",
            "GLOSSARY",
        ]
    )
    for f in report.drafted:
        assert f.provenance is Provenance.DRAFTED and f.status is Status.UNVERIFIED
        assert f.source == "llm:script:m1"
        assert f.value["prompt_hash"].startswith("sha256:")
    join = next(f for f in report.drafted if f.kind.name == "JOIN")
    assert isinstance(join.subject, JoinRef)
    assert join.subject.right[0] == ObjectRef(None, S, "customers", "customer_id")
    assert join.value["match_rate"] is None  # unknown until measured
    stored = {f.id for f in shop_store.facts_for(None, None)}
    assert {f.id for f in report.drafted} <= stored


def test_small_tables_are_batched(shop_store: SQLiteStore) -> None:
    script = Script()
    refs = [CUST, ACC, PROD, ORD]  # orders has 6 columns: its own call
    report = draft_tables(refs, store=shop_store, provider=script.provider(), config=Config())
    sizes = sorted(len(c["payload"]["tables"]) for c in script.calls)
    assert sizes == [1, 3]
    assert report.calls == 2 and report.errors == []


def test_invalid_reply_retried_then_error(shop_store: SQLiteStore) -> None:
    script = Script(lambda tables, n: {"tables": [{"table": "shop.customers"}]})
    report = draft_tables([CUST], store=shop_store, provider=script.provider(), config=Config())
    assert len(script.calls) == 2  # retried once
    assert report.drafted == []
    [err] = report.errors
    assert err.status is Status.ERROR and err.provenance is Provenance.DRAFTED
    assert err.value["code"] == "SG401"
    assert [d.code for d in report.diagnostics] == ["SG401"]
    assert shop_store.facts_for([CUST], ["USE_CASES"]) == []  # never partially applied


def test_retry_succeeds_second_time(shop_store: SQLiteStore) -> None:
    def answer(tables: list[dict[str, Any]], n: int) -> Any:
        return "not json" if n == 1 else {"tables": [table_reply("shop.customers")]}

    script = Script(answer)
    report = draft_tables([CUST], store=shop_store, provider=script.provider(), config=Config())
    assert len(script.calls) == 2 and report.errors == [] and report.drafted


def test_unknown_column_in_reply_is_invalid(shop_store: SQLiteStore) -> None:
    bad = table_reply("shop.customers", pii=[{"column": "no_such", "category": "email"}])
    script = Script(lambda tables, n: {"tables": [bad]})
    report = draft_tables([CUST], store=shop_store, provider=script.provider(), config=Config())
    assert len(script.calls) == 2 and [e.value["code"] for e in report.errors] == ["SG401"]


def test_batch_retries_only_failed_tables(shop_store: SQLiteStore) -> None:
    def answer(tables: list[dict[str, Any]], n: int) -> dict[str, Any]:
        names = [t["table"] for t in tables]
        if n == 1:  # drop accounts from the first reply
            return {"tables": [table_reply(t) for t in names if t != "shop.accounts"]}
        return {"tables": [table_reply(t) for t in names]}

    script = Script(answer)
    report = draft_tables(
        [CUST, ACC], store=shop_store, provider=script.provider(), config=Config()
    )
    assert [len(c["payload"]["tables"]) for c in script.calls] == [2, 1]
    assert report.errors == []


def test_provider_exception_is_error_fact(shop_store: SQLiteStore) -> None:
    def boom(*a: Any) -> Any:
        raise RuntimeError("connection reset")

    report = draft_tables(
        [CUST], store=shop_store, provider=CallableProvider(boom), config=Config()
    )
    assert [e.value["code"] for e in report.errors] == ["SG403"]


def test_unknown_table_is_reported(shop_store: SQLiteStore) -> None:
    script = Script()
    report = draft_tables(
        [ObjectRef(None, S, "nope")], store=shop_store, provider=script.provider(), config=Config()
    )
    assert script.calls == [] and [d.code for d in report.diagnostics] == ["SG404"]


def test_draft_never_overwrites_human(shop_store: SQLiteStore) -> None:
    human = Fact.make(
        FactKind.DESCRIPTION, CUST, {"text": "Our customers."}, Provenance.HUMAN, Status.UNVERIFIED
    )
    shop_store.put_facts([human])
    draft_tables([CUST], store=shop_store, provider=Script().provider(), config=Config())
    descs = shop_store.facts_for([CUST], ["DESCRIPTION"])
    assert human in descs and any(f.provenance is Provenance.DRAFTED for f in descs)
    eff = resolve(descs, now=AT, ttl_days=14)
    assert eff[("DESCRIPTION", "shop.customers")].primary == human


def test_token_budget_stops_run(shop_store: SQLiteStore) -> None:
    def answer(tables: list[dict[str, Any]], n: int) -> Any:
        return {"tables": [table_reply(t["table"]) for t in tables]}, Usage(900, 200)

    calls: list[int] = []

    def fn(system: str, user: str, schema: dict[str, Any], max_tokens: int) -> Any:
        tables = json.loads(user)["tables"]
        calls.append(len(tables))
        return answer(tables, len(calls))

    refs = [ORD, ObjectRef(None, S, "events"), ObjectRef(None, S, "sessions")]
    report = draft_tables(
        refs,
        store=shop_store,
        provider=CallableProvider(fn),
        config=Config(),
        budget=Budget(draft_budget_tokens=1100),
    )
    assert len(calls) == 1
    assert report.input_tokens == 900 and report.output_tokens == 200
    assert len(report.skipped) == len(refs) - calls[0] >= 1
    assert "SG402" in [d.code for d in report.diagnostics]
    assert shop_store.ledger_total("draft", "tokens", None) == 1100


def test_reply_schema_is_valid_json_schema_subset() -> None:
    assert validate({"tables": [table_reply("shop.x")]}, DRAFT_SCHEMA) == []
    assert validate({"tables": [{"table": "x"}]}, DRAFT_SCHEMA)
    bad = table_reply("x", use_cases=["q"] * 6)
    assert any("more than 5" in e for e in validate({"tables": [bad]}, DRAFT_SCHEMA))


def test_draft_codes_registered() -> None:
    from pathlib import Path

    from schemaguru.diagnostics import CODES

    doc = (Path(__file__).resolve().parents[3] / "docs/reference/diagnostics.md").read_text(
        encoding="utf-8"
    )
    for code in ("SG401", "SG402", "SG403", "SG404", "SG405"):
        assert code in CODES and f"| {code} |" in doc


def test_drafting_never_runs_from_scoping_or_rendering() -> None:
    """Spec 10.4: the query path (scope, render, agent tools) never imports the drafting run."""
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parents[3] / "src" / "schemaguru"
    offenders = []
    for pkg in ("scope", "render"):
        for py in sorted((src / pkg).rglob("*.py")) if (src / pkg).exists() else []:
            for node in ast.walk(ast.parse(py.read_text(encoding="utf-8"))):
                mods = (
                    [a.name for a in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module or ""]
                    if isinstance(node, ast.ImportFrom)
                    else []
                )
                offenders += [f"{py.name}: {m}" for m in mods if m.startswith("schemaguru.draft")]
    assert offenders == []
