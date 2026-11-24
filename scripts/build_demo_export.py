"""Export the shop fixture's knowledge for the documentation site's demo.

Writes ``site-demo/shop_export.json``: what SchemaGuru knows about the shop warehouse after a
crawl, a measurement run (the test ``FakeBackend``) and the spec 14.1 overlay, as the agent
tools render it. Every text in the file is real library output, passed through the library's
own value policy (masked mode, the default); no raw sample value is exported beyond what a
rendered view shows. The file is byte-deterministic: CI regenerates it before building the
site, and ``tests/conformance/test_docs_generated.py`` fails when the committed copy is stale.

Usage: ``python scripts/build_demo_export.py [--out PATH]``
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _shop_docs import (
    QUESTIONS,
    ROOT,
    ShopGuru,
    naive_counts,
    schema_dump,
    shop_guru,
)

import schemaguru as sg
from schemaguru.render.legend import LEGEND

OUT = ROOT / "site-demo" / "shop_export.json"


def _round(x: Any) -> Any:
    return None if x is None else round(float(x), 6)


def layout(nodes: list[str], links: list[tuple[str, str]]) -> dict[str, list[float]]:
    """A deterministic layered layout in [0, 1]^2 (``links`` are child -> parent).

    Each connected component is layered by the longest child -> parent chain (parents on top),
    ordered within a layer by barycentre sweeps (ties by name). The largest component fills the
    top band; the others, and isolated tables, share a bottom row.
    """
    adj: dict[str, set[str]] = {n: set() for n in nodes}
    parents: dict[str, set[str]] = {n: set() for n in nodes}
    for child, parent in links:
        adj[child].add(parent)
        adj[parent].add(child)
        parents[child].add(parent)
    comps: list[list[str]] = []
    seen: set[str] = set()
    for n in sorted(nodes):
        if n in seen:
            continue
        stack, comp = [n], []
        seen.add(n)
        while stack:
            x = stack.pop()
            comp.append(x)
            for y in sorted(adj[x]):
                if y not in seen:
                    seen.add(y)
                    stack.append(y)
        comps.append(sorted(comp))
    comps.sort(key=lambda c: (-len(c), c[0]))

    def level(n: str, guard: frozenset[str] = frozenset()) -> int:
        ups = [p for p in parents[n] if p not in guard]
        return 0 if not ups else 1 + max(level(p, guard | {n}) for p in ups)

    def arrange(comp: list[str]) -> dict[str, tuple[float, int]]:
        layers: dict[int, list[str]] = {}
        for n in comp:
            layers.setdefault(level(n), []).append(n)
        order = {k: sorted(v) for k, v in layers.items()}
        x: dict[str, float] = {}

        def place() -> None:
            for row in order.values():
                for i, n in enumerate(row):
                    x[n] = (i + 1) / (len(row) + 1)

        place()
        keys = sorted(order)
        for sweep in (keys[1:], list(reversed(keys[:-1])), keys[1:]):
            for k in sweep:
                order[k].sort(
                    key=lambda n: (
                        sum(x[m] for m in adj[n]) / len(adj[n]) if adj[n] else 0.5,
                        n,
                    )
                )
                place()
        return {n: (x[n], level(n)) for n in comp}

    pos: dict[str, list[float]] = {}
    main, rest = comps[0], comps[1:]
    placed = arrange(main)
    depth = max(lv for _, lv in placed.values()) if placed else 0
    band = 0.6  # the main component's share of the height
    for n, (fx, lv) in placed.items():
        pos[n] = [round(fx, 4), round(band * (lv / depth if depth else 0.5), 4)]
    if rest:
        width = sum(len(c) for c in rest)
        cursor = 0
        for comp in rest:
            local = arrange(comp)
            ldepth = max(lv for _, lv in local.values())
            for n, (fx, lv) in local.items():
                px = (cursor + fx * len(comp)) / width
                py = 0.8 + (0.2 * lv / ldepth if ldepth else 0.1)
                pos[n] = [round(px, 4), round(py, 4)]
            cursor += len(comp)
    return pos


def export(shop: ShopGuru) -> dict[str, Any]:
    guru = shop.guru
    tables = sorted(t.ref.canonical() for t in guru.store.iter_tables())
    graph = guru._current_graph()
    cfg = guru.config.graph
    edges: list[dict[str, Any]] = []
    for e in graph.edges:
        v = e.value
        edges.append(
            {
                "from": ", ".join(r.canonical() for r in e.join.left),
                "to": ", ".join(r.canonical() for r in e.join.right),
                "left_table": e.left_table.canonical(),
                "right_table": e.right_table.canonical(),
                "match_rate": _round(v.get("match_rate")),
                "coverage": _round(v.get("coverage")),
                "fan_in": _round(v.get("fan_in")),
                "cardinality": v.get("cardinality"),
                "verdict": e.verdict,
                "estimated": e.estimated,
                "attribution": sg.graph.is_attribution(e, cfg),
            }
        )
    links = sorted({(e["left_table"], e["right_table"]) for e in edges})
    hubs = [{"table": t.canonical(), "reason": r} for t, r in graph.hubs.reasons]

    out_tables = []
    for name in tables:
        ref = sg.ObjectRef.parse(name)
        t = guru.store.table(ref)
        cols = guru.store.columns(ref)
        desc = guru.handle_tool_call("describe_table", {"table": name})
        out_tables.append(
            {
                "name": name,
                "rows": None if t is None else t.row_count,
                "columns": [{"name": c.ref.column, "type": c.native_type} for c in cols],
                "block": desc["text"],
            }
        )

    paths: dict[str, Any] = {}
    for a in tables:
        for b in tables:
            if a == b:
                continue
            res = guru.handle_tool_call("find_join_path", {"from": a, "to": b})
            paths[f"{a}|{b}"] = {
                "refusal": res.get("refusal"),
                "text": res["text"],
                "tables": [p["tables"] for p in res.get("paths", [])],
            }

    dump = schema_dump(guru)
    counter = guru.counter
    questions = []
    for q in QUESTIONS:
        view = guru.context(q)
        before = shop.before.context(q)
        questions.append(
            {
                "question": q,
                "tables": [r.canonical() for r in view.tables],
                "view": view.text,
                "tokens": view.tokens,
                "fingerprint": view.fingerprint,
                "unmeasured_view": before.text,
            }
        )
    return {
        "format": "schemaguru-demo-export/1",
        "generated_by": "scripts/build_demo_export.py",
        "schemaguru_version": sg.__version__,
        "as_of": "2026-11-24",
        "value_policy": guru.config.sampling.llm_values,
        "note": (
            "The shop fixture warehouse (tests/fixtures/shop): crawled, measured exactly and "
            "overlaid with the spec 14.1 example YAML. Texts are SchemaGuru's own rendered "
            "output; values pass the masked value policy."
        ),
        "tables": out_tables,
        "edges": edges,
        "hubs": hubs,
        "layout": layout(tables, links),
        "paths": paths,
        "questions": questions,
        "schema_dump": dump,
        "schema_dump_tokens": counter.count(dump),
        "legend": LEGEND,
        "naive_counts": naive_counts(shop.conn),
    }


def render() -> str:
    with tempfile.TemporaryDirectory() as d, shop_guru(Path(d)) as shop:
        data = export(shop)
    return json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--out", type=Path, default=OUT)
    args = p.parse_args(argv)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(), encoding="utf-8", newline="\n")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
