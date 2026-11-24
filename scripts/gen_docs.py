"""Generate the documentation pages and snippets that must never be hand-written.

* ``docs/generated/*`` — real SchemaGuru output on the shop fixture (crawl, measurement through
  the test ``FakeBackend``, the spec 14.1 overlay), quoted by the landing page and the guides.
* ``docs/reference/cli.md`` — from the ``schemaguru`` argparse parser itself.
* ``docs/reference/diagnostics.md`` — from ``schemaguru.diagnostics.CODES``.

Everything is deterministic; ``tests/conformance/test_docs_generated.py`` regenerates into a
temporary directory and fails when a committed file is stale. Run after changing a renderer,
the CLI or a diagnostic code: ``python scripts/gen_docs.py``.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _shop_docs import ROOT, ShopGuru, block, naive_counts, schema_dump, shop_guru

DOCS = ROOT / "docs"


def _fence(text: str, lang: str = "text") -> str:
    return f"```{lang}\n{text.rstrip()}\n```\n"


def _lines(text: str, *needles: str) -> str:
    """The lines of ``text`` containing any of ``needles``, in order."""
    return "\n".join(ln for ln in text.splitlines() if any(n in ln for n in needles))


def shop_snippets(shop: ShopGuru) -> dict[str, str]:
    guru = shop.guru
    n = naive_counts(shop.conn)
    q = "monthly revenue by customer segment"
    after = guru.context(q).text
    before = shop.before.context(q).text
    orders = block(guru.context("refund rate by month").text, "shop.orders")
    hub = guru.handle_tool_call("find_join_path", {"from": "shop.orders", "to": "shop.events"})
    ok = guru.handle_tool_call("find_join_path", {"from": "shop.payments", "to": "shop.customers"})
    events_view = guru.context("events per account and session").text
    paths_part = events_view[events_view.index("Join paths") : events_view.index("\n\nNot shown")]
    blow_up = n["orders_x_accounts_x_events"] / n["orders"]
    out: dict[str, str] = {}

    out["story_hub.md"] = (
        "An agent asked for *events per order* joins `orders → accounts → events` on\n"
        '`account_id`. Both joins are "correct" (match rate 100%), yet the result has\n'
        f"**{n['orders_x_accounts_x_events']:,} rows for {n['orders']:,} orders** — "
        f"{blow_up:,.1f}× — because\n"  # noqa: RUF001
        "both tables attribute many rows to one account. No error, a plausible number.\n\n"
        "SchemaGuru measured both joins, saw `accounts` receiving two attribution edges, and\n"
        "refuses the route (`find_join_path` / `schemaguru path shop.orders shop.events`):\n\n"
        + _fence(hub["text"])
        + "\nA safe route is returned with its keys and its weakest support:\n\n"
        + _fence(ok["text"])
    )
    out["story_soft_delete.md"] = (
        "`orders.deleted` is TRUE or NULL — never FALSE. The filter an agent writes from the\n"
        f"name, `WHERE NOT deleted`, returns **{n['not_deleted']:,} rows**; the live rows are\n"
        f"**{n['live']:,}**. The measured values sit next to the human's soft-delete rule:\n\n"
        + _fence(_lines(orders, "live rows:", "deleted      BOOLEAN"))
    )
    out["story_case_drift.md"] = (
        f"`WHERE status = 'paid'` counts **{n['status_paid_exact']:,}** orders; "
        f"**{n['status_paid_any_case']:,}** are paid.\n"
        "The overlay declares `paid, refunded, cancelled`; measurement found `PAID` as well.\n"
        "SchemaGuru keeps the human expectation and states the contradiction (here under a\n"
        "waiver with an expiry date):\n\n" + _fence(_lines(orders, "status"))
    )
    out["quickstart_before.txt"] = before
    out["quickstart_after.txt"] = after
    out["orders_block.txt"] = orders
    out["events_paths.txt"] = paths_part
    out["prompt_block.txt"] = guru.prompt_block()
    out["schema_dump.txt"] = schema_dump(guru)
    tools = [{"name": t["name"], "description": t["description"]} for t in guru.tools()]
    out["tools.json"] = json.dumps(tools, indent=2, ensure_ascii=False)
    for name in ("agent", "planner", "verifier", "router"):
        view = guru.context("refund rate by month", profile=name, budget_tokens=1200)
        out[f"profile_{name}.txt"] = view.text
    out["search_refunds.txt"] = guru.handle_tool_call("search_tables", {"query": "refunds"})["text"]
    return {k: v if v.endswith("\n") else v + "\n" for k, v in out.items()}


# ----- reference pages ----------------------------------------------------------------------


def _cell(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _action_row(a: argparse.Action) -> str | None:
    if a.help == argparse.SUPPRESS or isinstance(a, argparse._HelpAction):
        return None
    if isinstance(a, argparse._SubParsersAction):
        return None
    if a.option_strings:
        name = ", ".join(f"`{s}`" for s in a.option_strings)
        if a.nargs != 0 and not isinstance(a, argparse._StoreTrueAction | argparse._VersionAction):
            name += f" `{(a.metavar or a.dest).upper()}`"
    else:
        name = f"`{a.metavar or a.dest}`"
        if a.nargs in ("*", "?"):
            name += " (optional)"
        elif a.nargs == "+":
            name += " (one or more)"
    parts = [a.help or ""]
    if a.choices:
        parts.append("one of " + ", ".join(f"`{c}`" for c in a.choices))
    default = a.default
    if (
        default not in (None, False, argparse.SUPPRESS)
        and not isinstance(a, argparse._VersionAction)
        and a.nargs not in ("*", "+")
        and "default" not in (a.help or "")
    ):
        parts.append(f"default `{default}`")
    if isinstance(a, argparse._AppendAction):
        parts.append("repeatable")
    return f"| {name} | {_cell('; '.join(p for p in parts if p))} |"


def cli_reference() -> str:
    from schemaguru.cli.main import _parser

    p = _parser()
    lines = [
        "# CLI reference",
        "",
        "<!-- generated by scripts/gen_docs.py from the argparse parser; do not edit -->",
        "",
        f"`{p.prog}` — {p.description}",
        "",
        "Exit codes: `0` ok · `1` review-class findings (refresh, lint) or a refused path · "
        "`2` invalid input · `3` errors or nothing measured.",
        "",
        "## Global options",
        "",
        "Given before or after the command.",
        "",
        "| Option | Meaning |",
        "|---|---|",
    ]
    lines += [r for a in p._actions if (r := _action_row(a)) is not None]
    sub = next(a for a in p._actions if isinstance(a, argparse._SubParsersAction))
    helps = {c.dest: c.help for c in sub._choices_actions}
    choices: Mapping[str, argparse.ArgumentParser] = sub.choices
    for name, sp in choices.items():
        lines += ["", f"## `{name}`", "", f"{helps.get(name, '')}", ""]
        rows = [r for a in sp._actions if (r := _action_row(a)) is not None]
        if rows:
            lines += ["| Argument | Meaning |", "|---|---|", *rows]
        else:
            lines.append("No arguments besides the global options.")
    return "\n".join(lines) + "\n"


def diagnostics_reference() -> str:
    from schemaguru.diagnostics import CODES

    lines = [
        "# Diagnostic codes",
        "",
        "Generated from `schemaguru.diagnostics.CODES`; a test asserts the two agree.",
        "",
        "| Code | Meaning |",
        "|---|---|",
    ]
    lines += [f"| {code} | {_cell(CODES[code])} |" for code in sorted(CODES)]
    return "\n".join(lines) + "\n"


def _default_text(value: object) -> str:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1 << 20:
        for unit, shift in (("TiB", 40), ("GiB", 30), ("MiB", 20)):
            if value % (1 << shift) == 0:
                return f"{value >> shift} {unit}"
        if value % 1000 == 0:
            return f"{value:_}"
    return repr(value)


def _type_text(hint: object) -> str:
    if isinstance(hint, type):
        return hint.__name__
    return str(hint).replace("typing.", "").replace("collections.abc.", "")


def config_reference() -> str:
    """Every configuration field with its type and default, from the dataclasses."""
    import dataclasses
    import typing

    import schemaguru as sg

    sections = [
        ("Config", "", sg.Config),
        ("Budget", "budget.", sg.Budget),
        ("SamplingConfig", "sampling.", sg.SamplingConfig),
        ("GraphConfig", "graph.", sg.GraphConfig),
        ("ScopeConfig", "scope.", sg.ScopeConfig),
        ("CrawlConfig", "crawl.", sg.CrawlConfig),
    ]
    lines: list[str] = []
    for title, prefix, cls in sections:
        hints = typing.get_type_hints(cls)
        lines += [f"### `{title}`", "", "| Key | Type | Default |", "|---|---|---|"]
        for f in dataclasses.fields(cls):
            if f.name in ("budget", "sampling", "graph", "scope", "crawl"):
                continue
            if f.default is not dataclasses.MISSING:
                default = _default_text(f.default)
            elif f.default_factory is not dataclasses.MISSING:
                default = _default_text(f.default_factory())
            else:
                default = "(required)"
            hint = _type_text(hints[f.name])
            lines.append(f"| `{prefix}{f.name}` | {_cell(hint)} | `{default}` |")
        lines.append("")
    return "\n".join(lines)


def generate() -> dict[str, str]:
    """``{path relative to docs/: content}`` for every generated file."""
    out = {
        "reference/cli.md": cli_reference(),
        "reference/diagnostics.md": diagnostics_reference(),
        "generated/config_reference.md": config_reference(),
    }
    with tempfile.TemporaryDirectory() as d, shop_guru(Path(d)) as shop:
        out.update({f"generated/{k}": v for k, v in shop_snippets(shop).items()})
    return out


def main() -> int:
    for rel, text in generate().items():
        path = DOCS / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote docs/{rel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
