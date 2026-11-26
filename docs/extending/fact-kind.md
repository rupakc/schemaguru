# Fact kind

Fact kinds are an open registry. Add one for knowledge the built-ins do not cover — an
owner, a data-retention class, a cost centre — and store it like any other fact.

## Protocol

```py
@dataclass(frozen=True)
class KindSpec:
    name: str
    subject: Literal["table", "column", "join", "global"]
    meaning: bool          # HUMAN beats DRAFTED (precedence rule 1)
    expectation: bool      # never replaced by measurement; checked (rule 2)
    identity_fields: tuple[str, ...] | None = None   # payload keys that define the fact id
```

`identity_fields=None` makes the whole value part of the fact's id; a tuple names only those
keys, so a re-measured number updates the same fact instead of creating another. Register
with `schemaguru.model.register_fact_kind(spec)` (a duplicate name raises unless
`replace=True`); `get_kind(name)` looks one up.

## Built-in kinds

| Kind | Subject | Identity fields |
|---|---|---|
| `DESCRIPTION`, `USE_CASES`, `ENTITY_TYPE`, `PITFALL` | table | whole value (meaning) |
| `GLOSSARY` | global | whole value (meaning) |
| `PRIMARY_KEY` | table | `columns` (expectation) |
| `JOIN` | join | none (expectation) |
| `VALUES` | column | `case_sensitive`, `split` (expectation) |
| `SOFT_DELETE` | table | `predicate` (expectation) |
| `NULL_RATE`, `DISTINCT` | column | none |
| `ROW_COUNT` | table | none |
| `FRESHNESS` | table | `column` |
| `TYPE_NOTE` | column | `stored_as`, `means` |
| `PII` | column | `category` |
| `METRIC`, `WAYPOINT_RULE` | global | whole value |
| `NEVER_QUERY` | table | whole value |
| `LINEAGE` | table | `upstream`, `ignore` |
| `COLUMN_LINEAGE` | column | `inputs`, `ignore` |

`LINEAGE` and `COLUMN_LINEAGE` are declared (dbt), parsed (view definitions) or written by a
human (overlay), and their status is always `UNVERIFIED`: a lineage fact that is MEASURED or
DRAFTED, or whose status is anything else, raises. The fact's `source` is not part of its id.
`schemaguru.model.resolve_lineage` applies their precedence per subject and level: a human list
replaces the parsed ones, a human `ignore` hides them (all of them, or only an exactly equal
list), and equal parsed lists from dbt and a view merge into one link naming both sources.

## Worked example: data owners

```python
from datetime import datetime, timezone

import schemaguru as sg
from schemaguru.model import Fact, KindSpec, Provenance, Status, register_fact_kind

OWNER = KindSpec("OWNER", "table", meaning=True, expectation=False, identity_fields=())
register_fact_kind(OWNER)

guru = sg.Guru.connect("duckdb:///warehouse.duckdb")
orders = guru.table_ref("orders")
guru.store.put_facts([Fact.make(OWNER, orders, {"team": "checkout"}, Provenance.HUMAN,
                                Status.UNVERIFIED, source="owners.csv:2",
                                observed_at=datetime(2026, 11, 24, tzinfo=timezone.utc))])
for f in guru.store.facts_for([orders], ["OWNER"]):
    print(f.kind.name, f.value["team"], f.provenance.value, f.source)
guru.close()
```

Custom kinds take part in resolution, snapshots, diffs and refresh reports like the
built-ins. To show one in views, render it in a [profile](profile-format.md).

## Test template

```py
def test_owner_kind_identity():
    a = Fact.make(OWNER, ref, {"team": "checkout"}, Provenance.HUMAN, Status.UNVERIFIED)
    b = Fact.make(OWNER, ref, {"team": "payments"}, Provenance.HUMAN, Status.UNVERIFIED)
    assert a.id == b.id             # identity_fields=(): one owner fact per table
    assert Fact.from_dict(a.to_dict()) == a
```
