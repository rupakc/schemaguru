# Demo

This page runs entirely in your browser, on a knowledge export of the **shop** warehouse —
the fixture SchemaGuru is tested on, with its known traps: a hub table two others attribute
to, a soft-delete flag that is TRUE or NULL, an enum that drifted in case, a thin but correct
join, a broken join. The export was produced by the real library (`scripts/build_demo_export.py`:
crawl, measurement, the [example overlay](guide/overlays.md)); every text below is the
library's own output under the default masked value policy. Nothing is sent anywhere.

<link rel="stylesheet" href="../demo-assets/demo.css">
<div id="sg-demo" markdown="0"><p>Loading the shop warehouse's knowledge…</p></div>
<script src="../demo-assets/demo.js"></script>

<noscript>The demo needs JavaScript. The same outputs are quoted on the
<a href="..">home page</a> and in the guides.</noscript>

What you are looking at:

* **Ask a question** shows `guru.context(question)` exactly as an agent would read it, for
  the suggested questions. Other questions are answered by a keyword match in your browser,
  which is labelled as an approximation — the library's ranking (full-text search, glossary
  hits, join expansion) is not re-implemented here.
* **Join-graph explorer** draws the measured joins (child → parent). Pick two tables to see
  `find_join_path`'s answer: a route with its join keys and weakest support, or a refusal
  that names its reason. Try *orders → events*: both joins into `accounts` match 100%, and the
  route is still refused, because it would pair every order with every event of an account.
* **Before and after** compares the common "paste the schema" prompt with the scoped view.

Run the same thing locally: `python examples/duckdb_quickstart.py`.

A note on the demo's graph: the shop fixture is built in DuckDB with primary keys only (its
`_create` emits no `FOREIGN KEY` clauses), because its data is deliberately dirty (20 orphan
customer keys, a broken child table) and an enforced foreign key would reject those rows. So the
export has no *declared* edges, only measured ones: the explorer shows a coincidental
primary-key-to-primary-key `child_id` join between `broken_child` and `ref_child`, and lacks the
intended `parent_id` to `id` edges into `ref_parent`. This is an artefact of the fixture, not of SchemaGuru: a warehouse with
declared foreign keys contributes them as declared edges.
