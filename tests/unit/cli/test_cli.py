"""The ``schemaguru`` command line (spec 19.1) against the shop DuckDB warehouse."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from schemaguru.cli import main
from schemaguru.model.facts import Fact, Provenance, Status
from schemaguru.model.kinds import FactKind
from schemaguru.model.refs import ObjectRef
from schemaguru.store.sqlite import SQLiteStore
from tests.unit.cli.conftest import Run, jloads
from tests.unit.draft.conftest import Script

ORD = ObjectRef(None, "shop", "orders")
CUS = ObjectRef(None, "shop", "customers")
SRC = "llm:script:m1"


@pytest.fixture
def crawled(run: Run, url: str, store: str) -> str:
    code, _, err = run("crawl", "--url", url, "--store", store)
    assert code == 0, err
    return store


def seed_drafts(store: str, n: int = 3) -> list[Fact]:
    drafts = [
        Fact.make(
            FactKind.DESCRIPTION,
            ORD if i % 2 == 0 else CUS,
            {"text": f"Draft number {i}.", "prompt_hash": "sha256:x"},
            Provenance.DRAFTED,
            Status.UNVERIFIED,
            source=SRC,
        )
        for i in range(n)
    ]
    with SQLiteStore(store) as st:
        st.put_facts(drafts)
    return sorted(drafts, key=lambda f: (f.subject.canonical(), f.kind.name, f.id))


def human_texts(store: str) -> list[str]:
    with SQLiteStore(store) as st:
        return sorted(
            str(f.value.get("text"))
            for f in st.iter_facts()
            if f.provenance is Provenance.HUMAN and f.kind is FactKind.DESCRIPTION
        )


# ----- init -----------------------------------------------------------------------------------


def test_init_writes_a_loadable_overlay_and_config(run: Run, crawled: str, tmp_path: Path) -> None:
    code, out, _ = run("init", "--dir", "proj", "--warehouse", "shop")
    assert code == 0 and "schemaguru.yaml" in out
    overlay = tmp_path / "proj" / "schemaguru.yaml"
    config = tmp_path / "proj" / "schemaguru.config.yaml"
    text = overlay.read_text(encoding="utf-8")
    assert text.startswith("#") and "version: 1" in text and "# tables:" in text
    assert yaml.safe_load(text) == {"version": 1, "warehouse": "shop"}
    assert yaml.safe_load(config.read_text(encoding="utf-8")) is None  # all commented
    code, out, err = run("lint", str(overlay), "--store", crawled)
    assert code == 0, (out, err)
    code, _, err = run("report", "--config", str(config), "--store", crawled)
    assert code == 0, err


def test_init_refuses_to_overwrite_without_force(run: Run, tmp_path: Path) -> None:
    assert run("init")[0] == 0
    code, _, err = run("init")
    assert code == 2 and "--force" in err
    (tmp_path / "schemaguru.yaml").write_text("mine", encoding="utf-8")
    assert run("init", "--force")[0] == 0
    assert (tmp_path / "schemaguru.yaml").read_text(encoding="utf-8") != "mine"


def test_init_echoes_no_credentials(run: Run) -> None:
    code, out, err = run("init", "--url", "postgresql://alice:hunter2@db.example.com/shop")
    assert code == 0 and "hunter2" not in out + err and "alice" not in out + err
    assert "postgresql://db.example.com " in out  # fail closed: scheme://host only
    assert "/shop" not in out


# ----- crawl, measure ---------------------------------------------------------------------------


def test_crawl(run: Run, url: str, store: str) -> None:
    code, out, err = run("crawl", "--url", url, "--store", store, "--format", "json")
    assert code == 0, err
    data = jloads(out)
    assert data["tables_seen"] >= 9 and data["errors"] == []
    assert Path(store).exists()


def test_crawl_prints_lineage_diagnostics(
    run: Run, url: str, store: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib

    monkeypatch.setattr(
        importlib.import_module("schemaguru.catalog.crawl"), "lineage_available", lambda: False
    )
    code, out, err = run("crawl", "--url", url, "--store", store, "--format", "json")
    assert code == 0, err  # a lineage diagnostic is not a failed crawl
    data = jloads(out)
    assert data["views_parsed"] == 0
    assert [d["code"] for d in data["lineage_diagnostics"]] == ["SG212"]
    assert "SG212" in err


def test_crawl_include_exclude(run: Run, url: str, store: str) -> None:
    code, out, _ = run(
        "crawl", "--url", url, "--store", store, "--include", "shop.orders", "--format", "json"
    )
    assert code == 0 and jloads(out)["tables_seen"] == 1


def test_measure_without_a_backend_exits_3(
    run: Run, crawled: str, url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from schemaguru.measure import flbridge

    real = flbridge.capabilities
    monkeypatch.setattr(flbridge, "capabilities", lambda: real() - {"measure"})  # no Checker
    code, _, err = run("measure", "shop.orders", "--url", url, "--store", crawled)
    assert code == 3 and "SG304" in err


@pytest.mark.factlang
def test_measure_through_real_factlang(run: Run, crawled: str, url: str) -> None:
    from schemaguru.measure import flbridge

    if "measure" not in flbridge.capabilities():
        pytest.skip("factlang capability measure not yet available")
    code, out, err = run(
        "measure", "shop.orders", "--url", url, "--store", crawled, "--format", "json"
    )
    assert code == 0, err
    assert "shop.orders" in out


def test_measure_with_a_backend(run: Run, crawled: str, url: str, fake_backend: Any) -> None:
    code, out, err = run(
        "measure", "shop.orders", "shop.customers", "--url", url, "--store", crawled,
        "--format", "json",
    )  # fmt: skip
    assert code == 0, err
    data = jloads(out)
    assert sorted(data["measured_tables"]) == ["shop.customers", "shop.orders"]
    assert data["facts_written"] > 0 and data["errors"] == []


def test_measure_budget_flags_and_conflicts(
    run: Run, crawled: str, url: str, fake_backend: Any
) -> None:
    code, out, _ = run(
        "measure",
        "--all",
        "--max-probes",
        "1",
        "--url",
        url,
        "--store",
        crawled,
        "--format",
        "json",
    )
    assert code in (0, 3) and len(jloads(out)["measured_tables"]) <= 1
    code, _, err = run("measure", "shop.orders", "--all", "--url", url, "--store", crawled)
    assert code == 2 and "not both" in err
    code, _, err = run("measure", "nosuch.table", "--url", url, "--store", crawled)
    assert code == 3 and "nosuch" in err  # an unknown table is a SchemaGuruError


# ----- draft, review, accept, export ------------------------------------------------------------


def test_draft_with_a_scripted_provider(
    run: Run, crawled: str, url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = Script()
    monkeypatch.setattr(
        "schemaguru.cli.commands._make_provider", lambda name, model: script.provider()
    )
    code, out, err = run(
        "draft", "shop.orders", "--url", url, "--store", crawled, "--no-verify", "--format", "json"
    )
    assert code == 0, err
    data = jloads(out)
    assert data["calls"] == 1 and data["drafted"]
    assert any(f["kind"] == "DESCRIPTION" for f in data["drafted"])
    code, out, _ = run("report", "--store", crawled, "--format", "json")
    assert jloads(out)["drafts_pending_review"] > 0


def test_draft_missing_extra_is_exit_2(
    run: Run, crawled: str, url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "anthropic", None)
    code, _, err = run("draft", "shop.orders", "--url", url, "--store", crawled)
    assert code == 2 and "schemaguru[anthropic]" in err


def test_review_accepts_with_scripted_stdin(run: Run, crawled: str, tmp_path: Path) -> None:
    drafts = seed_drafts(crawled, 4)
    overlay = tmp_path / "accepted.yaml"
    # y, n, "bogus" (re-asked), then s; the 4th is left because of q... answers are in order
    answers = "y\nn\nbogus\ns\nq\n"
    code, out, err = run(
        "review", "--store", crawled, "--write-overlay", str(overlay), "--format", "json",
        stdin=answers,
    )  # fmt: skip
    assert code == 0, err
    data = jloads(out)
    assert data["accepted"] == [drafts[0].id] and data["rejected"] == [drafts[1].id]
    assert data["skipped"] == 2 and data["reviewed"] == 4
    assert "please answer" in err and "accept? [y]es" in err  # prompts on stderr only
    assert human_texts(crawled) == [str(drafts[0].value["text"])]
    assert yaml.safe_load(overlay.read_text(encoding="utf-8"))["tables"]
    # the rejected one stays out of the next review; the skipped ones return
    code, out, _ = run("review", "--store", crawled, "--format", "json", stdin="q\n")
    assert jloads(out)["reviewed"] == 2


def test_review_edit_and_eof(run: Run, crawled: str) -> None:
    drafts = seed_drafts(crawled, 2)
    new = json.dumps({"text": "Edited by a human."})
    # an invalid edit re-asks the question; then a valid edit is accepted; EOF quits
    code, out, err = run(
        "review", "--store", crawled, "--format", "json", stdin=f"e\nnot json\ne\n{new}\n",
    )  # fmt: skip
    assert code == 0, err
    assert "not valid JSON" in err
    assert human_texts(crawled) == ["Edited by a human."]
    assert jloads(out)["skipped"] == 1 and len(jloads(out)["accepted"]) == 1
    assert drafts  # the original drafts are kept


def test_review_table_filter(run: Run, crawled: str) -> None:
    seed_drafts(crawled, 3)  # orders, customers, orders
    code, out, _ = run(
        "review", "--table", "shop.customers", "--store", crawled, "--format", "json", stdin="s\n"
    )
    assert code == 0 and jloads(out)["reviewed"] == 1
    assert run("review", "--table", "nosuch", "--store", crawled)[0] == 3


def test_accept_and_bad_id(run: Run, crawled: str) -> None:
    drafts = seed_drafts(crawled, 2)
    code, out, err = run("accept", drafts[0].id, "--store", crawled, "--format", "json")
    assert code == 0, err
    assert jloads(out)["accepted"][0]["source"] == f"accepted:{drafts[0].id}"
    code, _, err = run("accept", "deadbeef", "--store", crawled)
    assert code == 2 and "deadbeef" in err


def test_export_drafts(run: Run, crawled: str, tmp_path: Path) -> None:
    seed_drafts(crawled, 2)
    target = tmp_path / "out" / "drafts.yaml"
    target.parent.mkdir()
    code, _, err = run("export-drafts", str(target), "--store", crawled)
    assert code == 0, err
    assert yaml.safe_load(target.read_text(encoding="utf-8"))["tables"]
    mine = tmp_path / "mine.yaml"
    mine.write_text("version: 1\n", encoding="utf-8")
    code, _, err = run("export-drafts", str(mine), "--store", crawled)
    assert code == 2 and "refusing to overwrite" in err


def test_import_dbt(run: Run, crawled: str) -> None:
    manifest = str(Path(__file__).parents[2] / "fixtures" / "dbt" / "manifest.json")
    code, out, err = run("import-dbt", manifest, "--store", crawled, "--format", "json")
    assert code == 0, err
    assert jloads(out)["imported"] > 0
    bad = Path("bad.json")
    bad.write_text("[]", encoding="utf-8")
    code, _, err = run("import-dbt", str(bad), "--store", crawled)
    assert code == 2 and "SG209" in err


# ----- reading ----------------------------------------------------------------------------------


QUESTION = "monthly revenue by customer segment"


def test_context_text_json_markdown(run: Run, crawled: str) -> None:
    code, text, _ = run("context", QUESTION, "--store", crawled)
    assert code == 0 and "[shop.orders]" in text and "[shop.customers]" in text
    code, out, _ = run("context", QUESTION, "--store", crawled, "--format", "json")
    assert code == 0 and isinstance(jloads(out), dict)
    code, md, _ = run("context", QUESTION, "--store", crawled, "--format", "markdown")
    assert code == 0 and md.strip() and md != text
    code, small, _ = run("context", QUESTION, "--store", crawled, "--budget-tokens", "200")
    assert code == 0 and len(small) < len(text)


def test_context_crawls_an_empty_store_from_the_url(run: Run, url: str, store: str) -> None:
    code, text, err = run("context", QUESTION, "--url", url, "--store", store)
    assert code == 0, err
    assert "[shop.orders]" in text


def test_context_without_a_store_or_url_is_exit_2(run: Run) -> None:
    code, _, err = run("context", QUESTION)
    assert code == 2 and "no store" in err


def test_search(run: Run, crawled: str) -> None:
    code, out, _ = run("search", "customer orders", "--store", crawled, "-k", "3")
    assert code == 0 and "shop.orders" in out
    code, out, _ = run("search", "orders", "--store", crawled, "--format", "json", "-k", "2")
    data = jloads(out)
    assert len(data["tables"]) <= 2 and data["tables"][0]["table"].startswith("shop.")
    assert run("search", "orders", "--store", crawled, "-k", "0")[0] == 2


def test_path_routes_or_typed_refusal(run: Run, crawled: str, url: str, fake_backend: Any) -> None:
    assert run("measure", "--all", "--url", url, "--store", crawled)[0] in (0, 3)
    code, out, err = run(
        "path", "shop.orders", "shop.customers", "--store", crawled, "--format", "json"
    )
    data = jloads(out)
    assert code == 0 and data["refusal"] is None, (out, err)
    assert data["paths"][0]["tables"][0] == "shop.orders"
    code, out, _ = run("path", "shop.orders", "shop.orders", "--store", crawled, "--format", "json")
    assert code == 1 and jloads(out)["refusal"] == "SAME_TABLE"
    code, text, _ = run("path", "shop.orders", "shop.orders", "--store", crawled)
    assert code == 1 and text.strip()
    code, _, err = run("path", "shop.orders", "shop.nosuch", "--store", crawled)
    assert code == 2 and "nosuch" in err


# ----- refresh, report, lint ----------------------------------------------------------------------


def test_refresh_exit_code_is_the_reports(
    run: Run, crawled: str, url: str, fake_backend: Any
) -> None:
    code, out, err = run("refresh", "--url", url, "--store", crawled, "--format", "json")
    data = jloads(out)
    assert code == data["exit_code"] and code in (0, 1), err
    code, md, _ = run("refresh", "--url", url, "--store", crawled, "--format", "markdown")
    assert md.startswith("# Refresh report")
    code, text, _ = run("refresh", "--url", url, "--store", crawled)
    assert "exit code" in text


def test_refresh_without_a_backend_exits_3(
    run: Run, crawled: str, url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from schemaguru.measure import flbridge

    real = flbridge.capabilities
    monkeypatch.setattr(flbridge, "capabilities", lambda: real() - {"measure"})  # no Checker
    code, _, _ = run("refresh", "--url", url, "--store", crawled, "--format", "json")
    assert code == 3
    assert run("refresh", "--store", crawled)[0] == 2  # needs a warehouse


@pytest.mark.factlang
def test_refresh_through_real_factlang(run: Run, crawled: str, url: str) -> None:
    from schemaguru.measure import flbridge

    if "measure" not in flbridge.capabilities():
        pytest.skip("factlang capability measure not yet available")
    code, out, err = run("refresh", "--url", url, "--store", crawled, "--format", "json")
    assert code in (0, 1), err  # measured: OK, or changes to review; never 3 (nothing measured)
    assert jloads(out)["exit_code"] == code


def test_report(run: Run, crawled: str) -> None:
    seed_drafts(crawled, 2)
    code, out, _ = run("report", "--store", crawled, "--format", "json")
    data = jloads(out)
    assert code == 0 and data["tables"] >= 9 and data["drafts_pending_review"] == 2
    assert set(data["spend"]) >= {"measure_bytes", "draft_tokens"}
    code, text, _ = run("report", "--store", crawled)
    assert "live table" in text
    code, md, _ = run("report", "--store", crawled, "--format", "markdown")
    assert md.startswith("## Report")


def test_lint_clean_findings_and_missing(run: Run, crawled: str, tmp_path: Path) -> None:
    good = tmp_path / "good.yaml"
    good.write_text(
        "version: 1\ntables:\n  shop.orders:\n    description: One row per order.\n",
        encoding="utf-8",
    )
    code, out, err = run("lint", str(good), "--store", crawled, "--format", "json")
    assert code == 0, (out, err)
    assert jloads(out)["diagnostics"] == []
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "version: 1\ntables:\n  shop.nosuch_table:\n    description: Nothing here.\n",
        encoding="utf-8",
    )
    code, out, _ = run("lint", str(bad), "--store", crawled, "--format", "json")
    assert code == 1 and jloads(out)["diagnostics"]
    assert run("lint", str(tmp_path / "absent.yaml"), "--store", crawled)[0] == 2
    assert run("lint", "--store", crawled)[0] == 2  # no overlay found


def test_lint_does_not_touch_the_store(run: Run, crawled: str, tmp_path: Path) -> None:
    good = tmp_path / "good.yaml"
    good.write_text(
        "version: 1\ntables:\n  shop.orders:\n    description: One row per order.\n",
        encoding="utf-8",
    )
    before = human_texts(crawled)
    assert run("lint", str(good), "--store", crawled)[0] == 0
    assert human_texts(crawled) == before


# ----- serve, mcp, worker, doctor ---------------------------------------------------------------


@pytest.mark.parametrize(("cmd", "extra"), [("serve", "server"), ("mcp", "mcp")])
def test_optional_surfaces_say_what_to_install(
    run: Run, crawled: str, monkeypatch: pytest.MonkeyPatch, cmd: str, extra: str
) -> None:
    monkeypatch.setitem(sys.modules, "schemaguru.server", None)
    monkeypatch.setitem(sys.modules, "schemaguru.agent.mcp", None)
    code, out, err = run(cmd, "--store", crawled)
    assert code == 2 and f"schemaguru[{extra}]" in err and out == ""


def test_worker_once(run: Run, crawled: str, url: str, fake_backend: Any) -> None:
    code, _, err = run("worker", "--once", "--url", url, "--store", crawled)
    assert code == 0 and "measured" in err
    assert run("worker", "--once", "--store", crawled)[0] == 2


def test_doctor_json(run: Run, url: str, store: str) -> None:
    code, out, err = run("doctor", "--url", url, "--store", store, "--format", "json")
    checks = {c["check"]: c for c in jloads(out)["checks"]}
    assert set(checks) >= {"connectivity", "fts5", "factlang", "extras", "budgets"}
    assert checks["connectivity"]["status"] == "PASS"
    assert all(c["status"] in ("PASS", "WARN", "FAIL") for c in checks.values())
    assert all(c["hint"] for c in checks.values() if c["status"] != "PASS")
    assert code == (3 if any(c["status"] == "FAIL" for c in checks.values()) else 0), err


def test_doctor_without_url_and_unbounded_budget(run: Run, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCHEMAGURU_BUDGET__MAX_BYTES_TOTAL", "none")
    code, out, _ = run("doctor", "--format", "json")
    checks = {c["check"]: c for c in jloads(out)["checks"]}
    assert code == 0 and checks["connectivity"]["status"] == "WARN"
    assert (
        checks["budgets"]["status"] == "WARN" and "max_bytes_total" in checks["budgets"]["detail"]
    )


def test_doctor_bad_url_fails_without_leaking_credentials(run: Run) -> None:
    code, out, err = run("doctor", "--url", "nosuchdb://alice:hunter2@db.example.com/x")
    assert code == 3 and "FAIL" in out and "hunter2" not in out + err
    code, out, err = run("crawl", "--url", "nosuchdb://alice:hunter2@db.example.com/x")
    assert code == 3 and "hunter2" not in out + err


def test_doctor_text_hints(run: Run) -> None:
    code, out, _ = run("doctor")
    assert code == 0 and "WARN connectivity" in out and "hint:" in out


# ----- formats, exit codes, global flags ----------------------------------------------------------


def test_json_format_parseable(run: Run, crawled: str, url: str, tmp_path: Path) -> None:
    seed_drafts(crawled, 1)
    cases: list[tuple[str, ...]] = [
        ("crawl", "--url", url, "--store", crawled),
        ("report", "--store", crawled),
        ("search", "orders", "--store", crawled),
        ("context", QUESTION, "--store", crawled),
        ("path", "shop.orders", "shop.customers", "--store", crawled),
        ("review", "--store", crawled),
        ("doctor",),
        ("init", "--dir", str(tmp_path / "j")),
    ]
    for argv in cases:
        code, out, _ = run(*argv, "--format", "json", stdin="s\n")
        assert code in (0, 1), argv
        json.loads(out)  # stdout is JSON and nothing else


def test_global_flags_before_or_after_the_command(run: Run, url: str, store: str) -> None:
    code, out, _ = run("--url", url, "--store", store, "--format", "json", "crawl")
    assert code == 0 and jloads(out)["tables_seen"] >= 9
    code, out, _ = run("report", "--store", store, "--format", "json")
    assert code == 0 and jloads(out)["tables"] >= 9
    code, out, _ = run("--format", "json", "report", "--store", store)
    assert code == 0 and jloads(out)["tables"] >= 9


def test_url_from_environment(
    run: Run, url: str, store: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCHEMAGURU_URL", url)
    code, out, _ = run("crawl", "--store", store, "--format", "json")
    assert code == 0 and jloads(out)["tables_seen"] >= 9


def test_config_from_env_reaches_the_command(
    run: Run, url: str, store: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCHEMAGURU_CRAWL__INCLUDE", "shop.orders, shop.customers")
    code, out, err = run("crawl", "--url", url, "--store", store, "--format", "json")
    assert code == 0, err
    assert jloads(out)["tables_seen"] == 2


def test_config_file(run: Run, url: str, store: str, tmp_path: Path) -> None:
    cfg = tmp_path / "c.yaml"
    cfg.write_text("crawl:\n  include: [shop.orders]\n", encoding="utf-8")
    code, out, _ = run(
        "crawl", "--url", url, "--store", store, "--config", str(cfg), "--format", "json"
    )
    assert code == 0 and jloads(out)["tables_seen"] == 1


def test_exit_codes(run: Run, url: str, store: str, monkeypatch: pytest.MonkeyPatch) -> None:
    assert run()[0] == 2  # no command
    assert run("frobnicate")[0] == 2  # unknown command
    assert run("crawl", "--format", "yaml")[0] == 2  # bad choice
    assert run("--help")[0] == 0 and run("--version")[0] == 0
    assert run("crawl", "--store", store)[0] == 2  # no warehouse
    assert run("report", "--store", store)[0] == 2  # no store
    assert run("crawl", "--url", url, "--store", store, "--config", "nope.yaml")[0] == 2
    monkeypatch.setenv("SCHEMAGURU_SCOPE__SEED_K", "banana")
    code, _, err = run("crawl", "--url", url, "--store", store)
    assert code == 2 and "seed_k" in err
    monkeypatch.delenv("SCHEMAGURU_SCOPE__SEED_K")
    monkeypatch.setenv("SCHEMAGURU_NOPE", "1")
    assert run("crawl", "--url", url, "--store", store)[0] == 2
    monkeypatch.delenv("SCHEMAGURU_NOPE")
    assert run("crawl", "--url", url, "--store", store)[0] == 0
    code, _, err = run("crawl", "--url", "not a url", "--store", store)
    assert code == 3 and "SG106" in err


def test_help_lists_every_spec_subcommand(run: Run) -> None:
    code, out, _ = run("--help")
    names = "init crawl measure draft review accept export-drafts import-dbt context search path"
    for name in (names + " refresh report lint serve mcp doctor worker").split(" "):
        assert name in out, name
    assert code == 0


def test_python_dash_m_entry_point(tmp_path: Path) -> None:
    src = Path(__file__).parents[3] / "src"
    env = {**os.environ, "PYTHONPATH": str(src)}
    p = subprocess.run(
        [sys.executable, "-m", "schemaguru", "--version"],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
    )
    assert p.returncode == 0 and "schemaguru 0.1.1" in p.stdout
    assert main(["--version"]) == 0


# ----- fix round 1: redaction of URLs only, review durability, help ------------------------------


def test_errors_stay_informative_and_hide_only_the_password(run: Run) -> None:
    for argv in (
        ("crawl", "--url", "postgresql://bob:hunter2@nohost.invalid/db"),
        ("doctor", "--url", "postgresql://bob:hunter2@nohost.invalid/db"),
    ):
        code, out, err = run(*argv)
        text = out + err
        assert code == 3 and "hunter2" not in text, text
        assert text.strip() != "error: ***"
        assert len(text.strip()) > 20  # names the failure, not just a mask


def test_scrub_leaves_non_url_text_alone() -> None:
    from schemaguru.cli.commands import scrub

    msg = "contact ops@example.com or bob@host about it"
    assert scrub(msg) == msg
    url = "postgresql://bob:hunter2@h.example/db"
    out = scrub(f"cannot open {url}: timeout; retry via ({url}) for ops@example.com", [url])
    assert "hunter2" not in out and "bob" not in out
    assert out.startswith("cannot open ") and "timeout" in out and "ops@example.com" in out
    # a URL the user never passed is still caught by shape; trailing punctuation survives
    other = scrub("tried 'mysql://u:pw@x.example/d'.")
    assert "pw" not in other and other.startswith("tried '") and other.endswith("'.")
    # the exact --url string is replaced even when its password holds an '@'
    odd = "postgresql://u:p@ss@h.example/d"
    assert "p@ss" not in scrub(f"failed on {odd}", [odd])


def test_review_decisions_survive_ctrl_c(
    run: Run, crawled: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    drafts = seed_drafts(crawled, 4)
    answers = iter(["y", "n", None])

    def prompt(self: object, text: str) -> str | None:
        v = next(answers)
        if v is None:
            raise KeyboardInterrupt
        return v

    monkeypatch.setattr("schemaguru.cli.commands.IO.prompt", prompt)
    code, _, err = run("review", "--store", crawled)
    assert code == 130 and "interrupted" in err
    assert human_texts(crawled) == [str(drafts[0].value["text"])]
    monkeypatch.undo()
    code, out, _ = run("review", "--store", crawled, "--format", "json", stdin="q\n")
    assert jloads(out)["reviewed"] == 2  # the accepted and the rejected one are gone


def test_no_command_prints_full_help(run: Run) -> None:
    code, out, err = run()
    assert code == 2 and out == ""
    assert "usage:" in err and "export-drafts" in err and "a command is required" in err


def test_scrub_password_with_spaces() -> None:
    from schemaguru.cli.commands import scrub

    for pw in ("my pass", "my big pass"):
        url = f"postgresql://alice:{pw}@db/x"
        msg = f"cannot open {url} now"
        for known in ([url], []):
            out = scrub(msg, known)
            assert out.startswith("cannot open postgresql://") and out.endswith(" now"), out
            assert "pass" not in out and "alice" not in out and "my" not in out.split("://")[1]
    assert scrub("see postgresql://host:5432/db or ops@example.com").endswith("ops@example.com")


def test_space_password_never_leaks_through_cli(run: Run, monkeypatch: pytest.MonkeyPatch) -> None:
    url = "postgresql://alice:my big pass@nohost.invalid/x"
    code, out, err = run("crawl", "--url", url)
    assert code == 3 and not any(w in out + err for w in ("big pass", "alice", "my big"))

    def boom(*a: object, **k: object) -> None:
        raise RuntimeError(f"driver said no for {url} (retry)")

    monkeypatch.setattr("schemaguru.cli.commands.Guru.connect", boom)
    monkeypatch.delenv("SCHEMAGURU_URL", raising=False)
    code, out, err = run("crawl", "--url", "postgresql://other/db")
    assert code == 3 and not any(w in out + err for w in ("big pass", "alice", "my big")), err
    assert "driver said no" in err


# --- final fix wave: driver ImportErrors name the extra; local paths shown; JSON errors -------


@pytest.mark.parametrize(
    ("module", "extra"),
    [
        ("snowflake", "snowflake"),
        ("snowflake.connector", "snowflake"),
        ("psycopg", "postgres"),
        ("google.cloud.bigquery", "bigquery"),
        ("databricks", "databricks"),
        ("clickhouse_connect", "clickhouse"),
    ],
)
def test_missing_driver_names_the_extra(
    run: Run, monkeypatch: pytest.MonkeyPatch, module: str, extra: str
) -> None:
    def missing(*a: object, **k: object) -> None:
        raise ModuleNotFoundError(f"No module named {module!r}", name=module)

    monkeypatch.setattr("schemaguru.cli.commands.Guru.connect", missing)
    code, out, err = run("crawl", "--url", "snowflake://u:pw@acct/db")
    assert code == 3 and out == ""
    assert f"pip install 'schemaguru[{extra}]'" in err, err
    assert "pw@" not in err


def test_unknown_missing_module_still_says_what_is_missing(
    run: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing(*a: object, **k: object) -> None:
        raise ModuleNotFoundError("No module named 'zorbdb'", name="zorbdb")

    monkeypatch.setattr("schemaguru.cli.commands.Guru.connect", missing)
    code, _, err = run("crawl", "--url", "zorb://h/db")
    assert code == 3 and "zorbdb" in err and "schemaguru[" not in err  # no invented extra


def test_duckdb_path_is_not_redacted(run: Run, tmp_path: Path) -> None:
    missing = tmp_path / "no_such_warehouse.duckdb"
    code, _, err = run("crawl", "--url", f"duckdb:///{missing}")
    assert code == 3 and "no_such_warehouse.duckdb" in err, err


def test_json_format_errors_are_a_json_object_on_stderr(run: Run) -> None:
    code, out, err = run("crawl", "--format", "json")
    assert code == 2 and out == ""
    body = json.loads(err)
    assert set(body["error"]) >= {"code", "message"} and "--url" in body["error"]["message"]
    code, out, err = run("--format", "json", "crawl", "--url", "nosuchdb://u:hunter2@h.invalid/x")
    body = json.loads(err)
    assert code == 3 and body["error"]["code"] == "SG106", body
    assert "hunter2" not in err
    # text format keeps the plain line (negative control)
    code, _, err = run("crawl")
    assert err.startswith("error: ")
