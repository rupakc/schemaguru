// The Pyodide spike: run the playground glue on the real wheels and store, under Node.
//
// Usage: node scripts/pyodide_check.mjs <pyodide_pkg_dir> <playground_dir>
//   <pyodide_pkg_dir>  an installed npm `pyodide` package (e.g. node_modules/pyodide)
//   <playground_dir>   PREP's output: both wheels, playground.db, playground.json, the overlay
//
// Prints one line per check and the wall time of each call; exits non-zero on any exception.
// Pyodide fetches packages it does not bundle (sqlite3, pyyaml) from its CDN.

import { readFileSync, readdirSync } from "node:fs";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";

const [pkgDir, playDir] = process.argv.slice(2);
if (!pkgDir || !playDir) {
  console.error("usage: node scripts/pyodide_check.mjs <pyodide_pkg_dir> <playground_dir>");
  process.exit(2);
}
const here = new URL(".", import.meta.url).pathname;
const timings = [];

async function timed(label, fn) {
  const t0 = performance.now();
  const out = await fn();
  timings.push([label, performance.now() - t0]);
  return out;
}

try {
  const { loadPyodide } = await import(pathToFileURL(resolve(pkgDir, "pyodide.mjs")).href);
  const py = await timed("loadPyodide", () => loadPyodide({ indexURL: resolve(pkgDir) + "/" }));
  await timed("loadPackage sqlite3+pyyaml", () => py.loadPackage(["sqlite3", "pyyaml"]));

  const files = readdirSync(playDir);
  const wheels = files.filter((f) => f.endsWith(".whl")).sort();
  if (wheels.length !== 2) throw new Error(`expected 2 wheels in ${playDir}, found ${wheels}`);
  await timed("unpack wheels", async () => {
    for (const w of wheels) py.unpackArchive(new Uint8Array(readFileSync(join(playDir, w))), "wheel");
  });
  const manifest = JSON.parse(readFileSync(join(playDir, "playground.json"), "utf8"));
  py.FS.mkdirTree("/playground");
  for (const f of ["playground.json", manifest.store, manifest.overlay]) {
    py.FS.writeFile(`/playground/${f}`, readFileSync(join(playDir, f)));
  }
  py.FS.writeFile("/home/pyodide/glue.py", readFileSync(join(here, "..", "site-playground", "glue.py")));

  console.log("python", py.runPython("import sys; sys.version.split()[0]"));
  console.log("pyodide", py.version);
  await timed("import glue", () => py.runPython("import glue"));
  const glue = py.pyimport("glue");
  console.log("wheels", wheels.join(" "), "imported; schemaguru", glue.SG_VERSION);

  py.runPython(`
import sqlite3
_c = sqlite3.connect(":memory:")
try:
    # not fts5 table x with column x: FTS5 rejects a column named like its table, anywhere
    _c.execute("CREATE VIRTUAL TABLE temp.fts5_probe USING fts5(x)")
    FTS5 = "yes"
except sqlite3.Error as e:
    FTS5 = f"no ({e})"
_c.close()
`);
  console.log("sqlite_version", py.runPython("sqlite3.sqlite_version"));
  console.log("fts5", py.globals.get("FTS5"));

  // smoke works on a copy; then the live calls run on the store itself, as the page does
  console.log("smoke", await timed("smoke", () => glue.smoke("/playground")));
  const show = (label, json) => {
    const data = JSON.parse(json);
    if (data.error) throw new Error(`${label}: ${data.text}`);
    return data;
  };
  const manifestJson = readFileSync(join(playDir, "playground.json"), "utf8");
  const opened = show(
    "sg_open",
    await timed("sg_open", () => glue.sg_open(`/playground/${manifest.store}`, manifestJson)),
  );
  console.log("sg_open", "search_backend", opened.search_backend, "tables", opened.tables.length);
  py.runPython(`
_j = sqlite3.connect("/playground/${manifest.store}")
JOURNAL = _j.execute("PRAGMA journal_mode").fetchone()[0]
_j.close()
`);
  console.log("journal_mode after open", py.globals.get("JOURNAL"));

  for (const q of [manifest.questions[0], "payments per customer"]) {
    const v = show("sg_context", await timed(`sg_context ${q}`, () => glue.sg_context(q)));
    const sql = v.text.split("\n").filter((l) => l.trim().startsWith("SQL:")).length;
    const overlay = v.text.includes("human overlay");
    console.log("sg_context", JSON.stringify(q), "tokens", v.tokens, "tables", v.tables.length,
      "sql_snippets", sql, "overlay_applied", overlay);
  }
  py.runPython(`
_v = glue._state["guru"].context("payments per customer")
SNIPPET_DIAGS = sorted({f"{d.code} {d.message}" for d in _v.diagnostics if d.code == "SG501"})
`);
  console.log("snippet diagnostics", JSON.stringify(py.globals.get("SNIPPET_DIAGS").toJs()));

  const hub = show("sg_path", await timed("sg_path hub", () => glue.sg_path("shop.orders", "shop.events")));
  console.log("sg_path shop.orders->shop.events refusal", hub.refusal);
  const ok = show("sg_path", await timed("sg_path ok", () => glue.sg_path("shop.payments", "shop.customers")));
  console.log("sg_path shop.payments->shop.customers refusal", ok.refusal, "paths", ok.paths.length);
  const lin = show("sg_lineage", await timed("sg_lineage", () =>
    glue.sg_lineage("analytics.revenue_by_segment", "upstream", 3)));
  console.log("sg_lineage nodes", lin.nodes.map((n) => n.ref).join(","));
  const m = show("sg_mask", await timed("sg_mask", () => glue.sg_mask("write to jane.doe@example.org")));
  console.log("sg_mask", JSON.stringify(m.text), "hits", m.hits);

  for (const [label, ms] of timings) console.log(`time ${label} ${ms.toFixed(0)} ms`);
} catch (e) {
  console.error("FAIL", e && e.stack ? e.stack : e);
  process.exit(1);
}
