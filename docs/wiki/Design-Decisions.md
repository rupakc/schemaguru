# Design Decisions

> SchemaGuru is a from-scratch reimplementation of ideas from an earlier internal schema layer (called "the predecessor" here); no code was copied, only lessons.
> Each rule below exists because breaking it once produced a measured wrong answer; together they are the checklist every change is reviewed against.
> The page also records the overall approach chosen and the alternatives that were rejected.

## The twenty rules, with reasons

**1. Unknown is `None`; never default a metric; never coerce `None` to 0 or 1.0.**
A missing measurement that silently becomes 0 reads as "no matches"; one that becomes 1.0 reads
as "perfect". Either way an agent acts on a number nobody measured. So "not measured" is never
0, never 1.0, never PASS, from storage to rendering.

**2. Methods travel with numbers; never compare across methods.**
An exact rate and a sampled estimate of the same thing are different kinds of evidence. A
comparison between them can manufacture a "change" or hide a real one. Every measured fact
carries its `method`, and numbers of different methods are never compared, nor does one replace
the other.

**3. Exact by default; large tables sampled representatively, never with `LIMIT`; sampled
numbers are `ESTIMATED`, labelled, never `VERIFIED`; a sample may refute but not confirm.**
In the predecessor a `LIMIT`-based "sample" returned rows in storage order and once overstated a
rate 3.3 times. Key-hash and block samples are representative, and their numbers come with a
fraction and an interval. A counter-example in a sample is real; the absence of one is not proof.

**4. Forward match and reverse coverage are distinct facts.**
A join was observed at 1.0 one way and 0.0002 the other. "Every payment has an order" says
nothing about "every order has a payment". Both directions are measured and shown.

**5. Thin joins are returned labelled; broken joins are refused.**
A narrow join is not a wrong join: a correct join between a big table and a rare event table is
thin by nature. Refusing it would refuse correct answers; hiding its thinness would mislead. So
it is offered last, marked `provisional`, with its coverage. A join that does not match (under
5%) is refused outright.

**6. The hub guard refuses a shape, not a node; banning nodes disconnected most questions.**
Joining two child tables through a shared parent multiplied rows 213 times in one observed case.
Banning hub tables outright disconnected most legitimate questions, because hubs are exactly the
tables everything joins to. The rule that survived refuses only the dangerous *shape*: in on
one many-to-one edge, out on another.

**7. No role is inferred from names alone; use values, shapes and cardinality.**
A frequency enum whose column name contained "email" was once hashed as personal data, and a role
column was hashed as an identity. Names lie. Detection looks at the values, their shapes and
their cardinality; a name can only lower a threshold.

**8. Expectations are alarms; never auto-edit them to match the data.**
If a person declares the values of a status column and the data has a new one, that difference
is the most valuable signal there is. Updating the declaration to match would silence the alarm.
Measurement contradicting an expectation is *drift*, reported, not "fixed".

**9. Measured types are stated next to human notes, never by deleting them.**
When a note says "date" and the column stores text, both are true statements about different
things (intent versus storage). The view shows the measured type and keeps the note.

**10. No writes on the query path.**
Knowledge that changes while an agent is answering changes its context mid-query, and an
in-memory change is lost on restart. Questions only queue work; feedback is queued; writes happen
in explicit runs and refreshes.

**11. Judge relative to the previous snapshot.**
Data moves between snapshots. A contradiction the previous snapshot already had is pre-existing
drift, not a regression of the new one. Judging absolutely would fail every nightly check for the
same old issue and bury the new ones.

**12. Byte-deterministic exports and views; fingerprint views.**
Agent behaviour changed when only the *order* of tables in its context changed. Identical inputs
therefore render identical bytes, and views carry a fingerprint so a host can see when its
agents' context changed.

**13. Consequence-ordered rendering; the legend states that silence is not verification.**
When a prompt is truncated, the end goes first. So warnings come before confirmations, truncation
drops confirmations and never warnings, and whenever a warning appears the legend says "Absence of
a warning is not verification."

**14. One bad item costs one item; loaders never raise on data.**
A single malformed overlay entry or failing probe must not take down a nightly run over a
hundred thousand tables. Each problem becomes a diagnostic for that item, and the run continues.

**15. Soft-delete flags may be TRUE/NULL, never FALSE; a load-bearing column is never dropped by a
name pattern.**
Real soft-delete columns are often TRUE for deleted rows and NULL otherwise, so `deleted = FALSE`
returns nothing. The live-rows predicate is measured and stated (`deleted IS NULL OR NOT
deleted`). And a column that a predicate or a join depends on must never be filtered out just
because its name matches an exclusion pattern.

**16. Composite keys are declared and grouped in full.**
Checking uniqueness or joining on part of a composite key reports duplicates and fan-out that
are not really there. Keys are kept, measured and rendered as the full ordered tuple.

**17. A narrower run never overwrites a fuller snapshot; missing checks are reported.**
A run that measured less (an error, a budget cap) must not make problems "disappear". Each
snapshot records the checks that ran; a check that did not run is `NOT_MEASURED`, not fixed. The
same idea in the store: per-column facts are all retained, because a last-write-wins index once
silently dropped 9 of 25 known mismatches.

**18. Waivers expire.**
An acknowledged contradiction without an end date becomes permanent silence. Every waiver needs
`expires`; an expired one is ignored and reported.

**19. Timestamps are timezone-aware UTC; naive is rejected.**
A naive timestamp is ambiguous by hours, which is enough to make freshness and staleness wrong.

**20. No proprietary names anywhere.**
SchemaGuru is generic open source. All examples use one invented shop, and a CI denylist scan
keeps the original project's names out of the repository.

## One more lesson: be honest about weak signals

The hop-cost fan-out term correlated only −0.30 with the true rows-per-key multiplication in the
predecessor. It stays, because nothing measured replaces it yet, and the documentation says so
([[Join-Graph]]). The guard that actually prevents row multiplication is the shape rule (6).

## The approach chosen

Three architectures were considered:

| Approach | Strength | Weakness |
|---|---|---|
| **A. Library + local knowledge store + human YAML overlays** | scales to hundreds of thousands of tables through an indexed store and scoped retrieval; human knowledge stays reviewable in git | two sources of knowledge, so strict precedence rules are needed |
| B. YAML files only (the predecessor's way) | simple and reviewable | gigabytes of YAML at scale, weak search, unreadable diffs |
| C. Server first | fits large shared deployments | adoption requires running a service, against "easy to integrate" |

**Chosen: A, with C as an optional add-on** (`schemaguru serve`, [[Server]]). Also
decided by the project owner: sample large (terabyte) tables representatively and measure small
ones exactly; depend on FactLang for all measurement; the LLM drafts meaning and humans augment or
override it, with the human always winning; values are `masked` by default and `raw` only on
opt-in; SchemaGuru is standalone and built from concepts, not code.

## See also

- [[Why-Schema-Intelligence]] - the failures these rules prevent
- [[Concepts]] - precedence and status
- [[Contributing]] - the self-review checklist in practice
- [[Architecture]]
