# Extending: FactKind

> Fact kinds are an open registry: you can add your own kind of statement (for example a table's owning team) and store facts of it alongside the built-in ones.
> A `KindSpec` says what the kind is about (table, column, join or global), whether it is meaning or an expectation, and which payload fields define a fact's identity.
> Register it once with `register_fact_kind`; custom kinds are stored, exported, diffed and resolved like built-ins, but the built-in profiles do not render them.

## The spec

```python
@dataclass(frozen=True)
class KindSpec:
    name: str
    subject: Literal["table", "column", "join", "global"]
    meaning: bool             # precedence like DESCRIPTION: HUMAN beats DRAFTED
    expectation: bool         # precedence like VALUES: checked, never replaced by measurement
    identity_fields: tuple[str, ...] | None = None   # None = the whole value is identity
```

`identity_fields` decides when two facts are "the same fact": with `("column",)`, re-measuring
replaces the old fact of the same column; with `None`, any change of value is a new fact. Keep
measured numbers *out* of identity, so re-measurement replaces rather than accumulates.

## Worked example

```python
from datetime import datetime, timezone
import schemaguru as sg
from schemaguru.model.kinds import KindSpec, register_fact_kind

OWNER = KindSpec("OWNER", subject="table", meaning=True, expectation=False)
register_fact_kind(OWNER)

fact = sg.Fact.make(
    OWNER,
    sg.ObjectRef(None, "shop", "orders"),
    {"team": "checkout"},
    sg.Provenance.HUMAN,
    sg.Status.UNVERIFIED,
    source="script:owners",
    observed_at=datetime(2026, 11, 24, tzinfo=timezone.utc),
)
print(fact.kind.name, fact.id[:15])
```

Real output: `OWNER sha256:b0e0d9ed` (the id is a content hash). The fact can be stored with
`guru.store.put_facts([fact])`, is part of exports, snapshots and refresh diffs (a change is
`REVIEW` by default, since no rule knows it), and is visible through
`guru.store.facts_for(None, ["OWNER"])`.

To show it to agents, add a profile that renders it ([[Extending-Profile-and-Format]]), reading
it from `TableKnowledge.get("OWNER")`.

## Test template

```python
def test_owner_kind_identity():
    a = sg.Fact.make(OWNER, ref, {"team": "checkout"}, sg.Provenance.HUMAN, sg.Status.UNVERIFIED)
    b = sg.Fact.make(OWNER, ref, {"team": "checkout"}, sg.Provenance.HUMAN, sg.Status.UNVERIFIED)
    c = sg.Fact.make(OWNER, ref, {"team": "growth"}, sg.Provenance.HUMAN, sg.Status.UNVERIFIED)
    assert a.id == b.id and a.id != c.id
```

Registering the same name twice raises `ValueError` unless `replace=True`.

## See also

- [[Concepts]]
- [[Extending-Profile-and-Format]]
- [[Extending-ChangeClassifier]]
- [[Store]]
