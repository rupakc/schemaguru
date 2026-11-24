# Join candidate source

A join is measured only if something proposes it. Candidates come from these sources, in
priority order (`schemaguru.measure.join_candidates`):

1. declared foreign keys from the catalog;
2. joins in overlays (`tables.<t>.joins`) and dbt `relationships` tests (`DECLARED`);
3. registered `JoinCandidateSource` plug-ins;
4. name and type heuristics: `x_id` / `xId` / `x_fk` → a table `x` or `xs` with a compatible
   key, and a column named like another table's single-column primary key in the same
   schema (at most 5 per column, same catalog only);
5. join candidates in LLM drafts.

## Protocol

`sg.measure.JoinCandidateSource`: a `name: str` and `candidates(store, table)`, returning
`JoinRef`s with `table` on the left (child) side. It only reads. Register one with
`sg.measure.register_join_candidate_source(source)`, or install it through the entry-point
group `schemaguru.join_candidate_sources`. A proposal is measured before anything is shown;
one that fails measurement leaves only a diagnostic, like a heuristic guess. A source that
raises costs only its proposals.

```python
import schemaguru as sg
from schemaguru.model import JoinRef, ObjectRef


class Lineage:
    name = "lineage"

    def candidates(self, store, table):
        if table.table == "ref_child":
            yield JoinRef((ObjectRef(None, "shop", "ref_child", "parent_id"),),
                          (ObjectRef(None, "shop", "ref_parent", "id"),))


sg.measure.register_join_candidate_source(Lineage())
```

Joins you also want shown as human expectations (a broken one is then a contradiction, not a
silent drop) belong in an overlay instead, as below.

## Worked example: a join the heuristics cannot see

In the shop, `ref_child.parent_id` points at `ref_parent.id`. No table is called `parent`,
so the name heuristics miss it, and the warehouse declares no foreign key. Propose it from a
generated overlay:

```python
from pathlib import Path

import schemaguru as sg

Path("generated-joins.yaml").write_text(
    "version: 1\n"
    "tables:\n"
    "  shop.ref_child:\n"
    "    joins:\n"
    "      - from: parent_id\n"
    "        to: shop.ref_parent.id\n"
    "        cardinality: N:1\n",
    encoding="utf-8",
)

guru = sg.Guru.connect("duckdb:///warehouse.duckdb")
print(guru.load_overlays("schemaguru.yaml", "generated-joins.yaml"))
guru.measure("shop.ref_child")
for f in guru.store.facts_for(None, ["JOIN"]):
    if f.subject.left[0].table == "ref_child" and f.subject.left[0].column == "parent_id":
        print(f.provenance.value, f.status.value, f.value.get("match_rate"),
              f.value.get("coverage"))
guru.close()
```

The human join stays as written (an *expectation*, never rewritten); measurement adds a
`MEASURED` join fact beside it — forward match 100%, reverse coverage 0.3% in the shop, a
correct but very asymmetric join — and only the measured fact makes it a path-search edge.

## Test template

```py
def test_generated_candidates_are_measured(shop_guru, tmp_path):
    overlay = tmp_path / "joins.yaml"
    overlay.write_text(render_overlay(my_join_source()), encoding="utf-8")
    assert not [d for d in shop_guru.load_overlays(overlay) if d.severity.value == "ERROR"]
    shop_guru.measure(all=True)
    joins = {(f.subject.left[0].column, f.provenance.value)
             for f in shop_guru.store.facts_for(None, ["JOIN"])}
    assert ("parent_id", "MEASURED") in joins
```
