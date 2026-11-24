from __future__ import annotations

from datetime import date
from pathlib import Path

from schemaguru.diagnostics import DiagnosticBag
from schemaguru.model.chains import FilterStep
from schemaguru.model.facts import Provenance
from schemaguru.model.joins import JoinRef
from schemaguru.model.refs import ObjectRef
from schemaguru.model.structure import Table
from schemaguru.overlays import load_overlays
from schemaguru.overlays.schema import check_filter_syntax
from schemaguru.store.sqlite import SQLiteStore

from .conftest import FIXTURES

TODAY = date(2026, 11, 24)


def load(store: SQLiteStore, tmp_path: Path, text: str, name: str = "o.yaml"):  # type: ignore[no-untyped-def]
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return load_overlays([p], store, today=TODAY)


def codes(bag: DiagnosticBag) -> list[str]:
    return [d.code for d in bag]


def test_full_example_from_spec_loads(store: SQLiteStore) -> None:
    facts, chains, waypoints, waivers, bag = load_overlays(
        [FIXTURES / "overlays" / "spec_example.yaml"], store, today=TODAY
    )
    assert list(bag) == []
    kinds = sorted(f.kind.name for f in facts)
    assert kinds == sorted(
        ["DESCRIPTION", "USE_CASES", "ENTITY_TYPE", "SOFT_DELETE", "PITFALL", "DESCRIPTION",
         "VALUES", "TYPE_NOTE", "JOIN", "GLOSSARY", "NEVER_QUERY"]
    )  # fmt: skip
    assert all(f.provenance is Provenance.HUMAN for f in facts)
    assert all(f.source is not None and f.source.startswith("overlay:") for f in facts)
    values = next(f for f in facts if f.kind.name == "VALUES")
    assert values.value["values"] == ["paid", "refunded", "cancelled"]
    assert values.value["split"] == ":"
    assert values.subject == ObjectRef(None, "shop", "orders", "status")
    join = next(f for f in facts if f.kind.name == "JOIN")
    assert isinstance(join.subject, JoinRef)
    assert join.subject.canonical() == "shop.orders.customer_id -> shop.customers.customer_id"
    assert join.value["cardinality"] == "N:1"
    assert len(chains) == 1 and chains[0].name == "order to payment provider"
    step = chains[0].steps[0]
    assert isinstance(step, JoinRef)
    assert step.canonical() == "shop.orders.order_id -> shop.payments.order_id"
    assert len(waypoints) == 1
    wp = waypoints[0]
    assert wp.scope == "pair" and wp.tables == (
        ObjectRef(None, "shop", "events"),
        ObjectRef(None, "shop", "orders"),
    )
    assert wp.require == ("edge(shop.events.session_id->shop.sessions.session_id)",)
    assert wp.forbid == () and wp.source.startswith("overlay:")
    assert len(waivers) == 1
    assert waivers[0].fact_pattern == "VALUES shop.orders.status"
    assert waivers[0].expires == date(2027, 1, 31)


def test_line_numbers_in_diagnostics(store: SQLiteStore, tmp_path: Path) -> None:
    text = (
        "version: 1\n"  # 1
        "tables:\n"  # 2
        "  shop.orders:\n"  # 3
        "    descriptoin: typo\n"  # 4
        "    columns:\n"  # 5
        "      nope:\n"  # 6
        "        description: x\n"  # 7
        "  shop.missing:\n"  # 8
        "    description: y\n"  # 9
    )
    facts, _, _, _, bag = load(store, tmp_path, text)
    by_code = {d.code: d for d in bag}
    assert by_code["SG202"].location == f"{tmp_path / 'o.yaml'}:4"
    assert by_code["SG202"].hint == "did you mean 'description'?"
    assert any(d.code == "SG203" and d.location.endswith(":6") for d in bag)  # type: ignore[union-attr]
    assert any(d.code == "SG203" and d.location.endswith(":8") for d in bag)  # type: ignore[union-attr]
    assert facts == []


def test_version_required(store: SQLiteStore, tmp_path: Path) -> None:
    for text in ("tables: {}\n", "version: 2\n", "version: true\n", "version:\n"):
        *_, bag = load(store, tmp_path, text)
        assert codes(bag) == ["SG201"], text


def test_overlay_key_ambiguous_case(tmp_path: Path, store: SQLiteStore) -> None:
    # exact spelling wins; a case-insensitive match that is not unique is SG204 naming candidates
    store.upsert_tables([Table(ObjectRef(None, "shop", n), "table") for n in ("Orders", "ORDERS")])
    facts, *_, bag = load(
        store, tmp_path, "version: 1\ntables:\n  shop.oRders:\n    description: d\n"
    )
    assert facts == []
    d = next(iter(bag))
    assert d.code == "SG204"
    assert "shop.ORDERS" in d.message and "shop.Orders" in d.message and "shop.orders" in d.message
    # exact still resolves
    facts, *_, bag = load(
        store, tmp_path, "version: 1\ntables:\n  shop.Orders:\n    description: d\n"
    )
    assert len(facts) == 1 and facts[0].subject == ObjectRef(None, "shop", "Orders")


def test_unique_case_insensitive_resolves(store: SQLiteStore, tmp_path: Path) -> None:
    facts, *_, bag = load(
        store, tmp_path, "version: 1\ntables:\n  SHOP.Customers:\n    description: d\n"
    )
    assert list(bag) == [] and facts[0].subject == ObjectRef(None, "shop", "customers")


def test_empty_key_is_absent(store: SQLiteStore, tmp_path: Path) -> None:
    text = (
        "version: 1\nwarehouse:\ntables:\n  shop.orders:\n    description:\n"
        "    use_cases:\n    pitfalls:\n    columns:\n    joins:\n"
        "chains:\nwaypoints:\nglossary:\nnever_query:\nwaivers:\n"
    )
    facts, chains, wps, waivers, bag = load(store, tmp_path, text)
    assert (facts, chains, wps, waivers) == ([], [], [], []) and list(bag) == []


def test_invalid_filter_sg205(store: SQLiteStore, tmp_path: Path) -> None:
    text = 'version: 1\ntables:\n  shop.orders:\n    soft_delete: "(deleted IS NULL"\n'
    facts, *_, bag = load(store, tmp_path, text)
    assert facts == [] and codes(bag) == ["SG205"]
    assert check_filter_syntax("a = 'x''y' AND (b)") is None
    assert check_filter_syntax("a; DROP") is not None
    assert check_filter_syntax("a = 'x") is not None
    assert check_filter_syntax("  ") is not None
    assert check_filter_syntax("a)") is not None


def test_chain_filter_and_note_steps(store: SQLiteStore, tmp_path: Path) -> None:
    text = (
        "version: 1\nchains:\n  - name: c\n    because: b\n    steps:\n"
        "      - shop.orders.customer_id -> shop.customers.customer_id\n"
        "      - {filter: \"status = 'paid'\", table: shop.orders}\n"
        "      - {note: keep only paid}\n"
    )
    _, chains, _, _, bag = load(store, tmp_path, text)
    assert list(bag) == [] and len(chains[0].steps) == 3
    assert isinstance(chains[0].steps[1], FilterStep)


def test_waivers(store: SQLiteStore, tmp_path: Path) -> None:
    text = (
        "version: 1\nwaivers:\n"
        "  - {fact: VALUES shop.a, reason: r}\n"
        "  - {fact: VALUES shop.b, reason: r, expires: 2026-01-01}\n"
        "  - {fact: VALUES shop.c, reason: r, expires: 2026-11-24}\n"
        "  - {fact: VALUES shop.d, reason: r, expires: soon}\n"
    )
    *_, waivers, bag = load(store, tmp_path, text)
    assert [w.fact_pattern for w in waivers] == ["VALUES shop.c"]
    assert codes(bag) == ["SG206", "SG207", "SG206"]
    *_, bag = load(
        store,
        tmp_path,
        "version: 1\nwaivers:\n  - {fact: nospace, reason: r, expires: 2030-01-01}\n",
    )
    assert codes(bag) == ["SG208"]


def test_waypoint_node_and_forbid(store: SQLiteStore, tmp_path: Path) -> None:
    text = (
        "version: 1\nwaypoints:\n  - node: shop.events\n    forbid: [shop.orders]\n"
        "    because: never direct\n"
    )
    _, _, wps, _, bag = load(store, tmp_path, text)
    assert list(bag) == [] and wps[0].scope == "node" and wps[0].forbid == ("shop.orders",)


def test_waypoint_node_without_forbid_list(store: SQLiteStore, tmp_path: Path) -> None:
    text = "version: 1\nwaypoints:\n  - node: shop.events\n    because: not a bridge\n"
    _, _, wps, _, bag = load(store, tmp_path, text)
    assert list(bag) == [] and wps[0].scope == "node" and wps[0].forbid == ()
    text = "version: 1\nwaypoints:\n  - pair: [shop.events, shop.orders]\n    because: b\n"
    _, _, wps, _, bag = load(store, tmp_path, text, "p.yaml")
    assert wps == [] and codes(bag) == ["SG208"]


def test_bad_inputs_never_raise(store: SQLiteStore, tmp_path: Path) -> None:
    cases = [
        "", "- a\n- b\n", "{{{", "version: 1\ntables: [a]\n",
        "version: 1\ntables:\n  shop.orders: 5\n", "version: 1\nchains: x\n", "&a [*a]",
        "version: 1\ntables:\n  '\"bad':\n    description: d\n",
        "version: 1\ntables:\n  shop.orders:\n    joins:\n      - {to: nope, from: x}\n",
    ]  # fmt: skip
    for i, text in enumerate(cases):
        load(store, tmp_path, text, f"c{i}.yaml")
    *_, bag = load_overlays([tmp_path / "missing.yaml"], store)
    assert codes(bag) == ["SG208"]


def test_directory_load(store: SQLiteStore, tmp_path: Path) -> None:
    d = tmp_path / "schemaguru"
    (d / "drafts").mkdir(parents=True)
    (d / "a.yaml").write_text(
        "version: 1\ntables:\n  shop.orders:\n    description: a\n", encoding="utf-8"
    )
    (d / "drafts" / "x.yaml").write_text(
        "version: 1\ntables:\n  shop.orders:\n    description: x\n", encoding="utf-8"
    )
    facts, *_ = load_overlays([d], store, today=TODAY)
    assert [f.value["text"] for f in facts] == ["a"]


def test_unquoted_numbers_keep_source_text(store: SQLiteStore, tmp_path: Path) -> None:
    text = (
        "version: 1\ntables:\n  shop.orders:\n    columns:\n      status:\n"
        "        values: [01, 007, 1.10, 0x1F, yes, '05', 2027-01-01]\n"
    )
    facts, *_, bag = load(store, tmp_path, text)
    assert list(bag) == []
    assert facts[0].value["values"] == ["01", "007", "1.10", "0x1F", "yes", "05", "2027-01-01"]
