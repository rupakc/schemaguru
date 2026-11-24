# PII and Sampling Policy

> The value policy decides what an LLM or an agent may see of your data values: `masked` (the default) replaces anything that looks personal with its shape, such as `<email>`; `raw` shows values only when you explicitly opt in; `none` shows no values at all, only counts.
> Detection is by the shape of the values first, then column-name hints, then cardinality, and never by a column's name alone.
> Masking is applied at every exit: when values are stored, in evidence, in error messages, in rendered views, in drafting payloads and in every agent tool result.

## The three modes

| Mode | What leaves SchemaGuru | Default |
|---|---|---|
| `masked` | observed values, with personal-looking ones replaced by shape tokens: `<email>`, `<phone>`, `<name>`, `<iban>`, `<card>`, `<ip>`, `<uuid>`, `<hex32>` (hex of that length), `<secret>`, `<address>`, `<free_text len=83>` | **yes** |
| `raw` | observed values unmasked, **only when you opt in** for those tables | opt-in |
| `none` | no values at all; names, types, comments and statistics only | available |

Set it globally and per table glob:

```python
cfg = sg.Config(sampling=sg.SamplingConfig(
    llm_values="masked",                       # the global default
    per_glob={"shop.products": "raw",          # product names are not personal: show them
              "shop.cust*": "none"},           # nothing from customer tables, ever
))
```

In `masked` mode a value is replaced only when it *looks* personal (it matches a detector's
shape); values with no personal shape, such as `paid`, `retail` or `42`, are shown as they are.
`none` hides every value, whatever it looks like, and leaves only counts.

When several globs match a table, the **narrowest** wins (`shop.cust*` is narrower than
`shop.*`, decided by real language containment, not by text). If several remain that are equal
or incomparable (`shop.*` vs `*.customers`), the **most restrictive** mode wins
(`none` > `masked` > `raw`), so an ambiguous configuration can never widen access. A result that
touches several tables (a join path, a tool result) uses the most restrictive mode of the tables
involved; `raw` only when every one of them is opted in.

## What gets masked, with examples

Real output of `schemaguru.pii.mask_value` and `sweep` (SchemaGuru 0.1.0, 2026-11-24):

```text
mask_value('user1@example.com') -> ('<email>', 'email')
mask_value('a@example.com; b@example.org') -> ('<email>', 'email')
mask_value('+49 30 1234567') -> ('<phone>', 'phone')
mask_value('DE89370400440532013000') -> ('<iban>', 'iban')
mask_value('4111 1111 1111 1111') -> ('<card>', 'card')
mask_value('10.0.0.12') -> ('<ip>', 'ip')
mask_value('550e8400-e29b-41d4-a716-446655440000') -> ('<uuid>', 'uuid')
mask_value('9f86d081884c7d659a2feaa0c55ad015') -> ('<hex32>', 'hex')
mask_value('Maria Garcia') -> ('<name>', 'name')
mask_value('maria garcia') -> ('maria garcia', None)
mask_value('Jean-Luc') -> ('Jean-Luc', None)
mask_value('White Paper') -> ('<name>', 'name')
mask_value('Price List') -> ('<name>', 'name')
mask_value('paid') -> ('paid', None)
mask_value('Ordered twice, the second parcel was returned to the depot unopened by the courier.') -> ('<free_text len=83>', 'free_text')
mask_value('Call me at user1@example.com') -> ('Call me at <email>', 'email')
sweep('contact user1@example.com or +49 30 1234567, ip 10.0.0.12') -> ('contact <email> or <phone>, ip <ip>', 3)
```

The detectors, in priority order: UUID, email (including lists), IPv4/IPv6, IBAN (checksum),
card (Luhn), long hex (20+ hex characters; 16 would corrupt JSON numbers), phone (E.164 and
national forms), JWT/secret-like strings, postal address heuristic, person-name heuristic, free
text (over 60 characters with spaces). The first match decides. Blank values (`''`, whitespace)
count as NULL.

### The person-name heuristic and its limits

A value is masked as `<name>` when it is two to four capitalised tokens and at least one token is
in a bundled list of names. This is deliberately **fail-safe**: product or document titles that
look like names are masked too ("White Paper", "Brown Sugar", "Price List" above), because
showing a real name is worse than hiding a product title. If that hurts a table, opt it into
`raw`, or disable the heuristic:

```python
from schemaguru.pii import register_detector
from schemaguru.pii.detectors import PersonNameDetector
register_detector(PersonNameDetector(enabled=False), replace=True)
```

Known limits: lower-case names (`maria garcia`), single tokens (`Jean-Luc`, a first name alone)
and hyphenated-only forms are **not** masked by this heuristic. Treat `masked` as a strong
default, not a guarantee; use `none` for tables that must never leak a value.

### Never by name alone

A column named `email_frequency` is not personal because of its name, and a column named `kind`
may well hold e-mail addresses. Classification (`schemaguru.pii.classify_column`) uses:

1. **Shape** of the observed values (the share matching a detector);
2. **Name hints** only lower the share needed; a name alone never classifies;
3. **Cardinality**: a column with at most 20 distinct values whose values look like hashes,
   e-mails, UUIDs or secrets is flagged `review` instead of auto-classified, because an
   anonymised enum looks like identity by shape and like an enum by cardinality.

Real output:

```text
PIIFinding(category='review', detector='email', reason='100% of observed values match the email shape, but only 5 distinct values: an anonymised enum looks like this; decide by hand')
PIIFinding(category='email', detector='email', reason='100% of observed values match the email shape')
```

The shop fixture's `anon_enum_t.kind` (five distinct hashed addresses on 10,000 rows) is exactly
this trap. Its view shows the shape, never a value:

```text
[shop.anon_enum_t]
  ⚠ id → shop.ref_parent.id WEAK: match 10% (measured 2026-11-24) — expect unmatched rows
  10,000 rows (measured 2026-11-24)
  Columns:
    id    INTEGER  → shop.ref_parent.id 1:1, match 10% (measured 2026-11-24); distinct 10,000 (measured 2026-11-24)
    kind  VARCHAR  PII email; values: <email> (10,000) +4 more (measured 2026-11-24); distinct 5 (measured 2026-11-24)
  Provenance: measured 2026-11-24
```

### What measurement can and cannot see

Values are observed only for low-cardinality columns (at most `enum_max_distinct`, 20, distinct
values) plus a few caller-supplied examples for drafting. A high-cardinality column such as
`customers.email` therefore has **no observed values** at all: nothing is shown, and nothing is
tagged PII by measurement either (see `email` in the `describe_table` output on
[[Rendering]]). A drafted `pii` guess, once accepted, tags it.

## Where masking happens

Values are masked at every point where they could leave:

1. **At storage.** Observed values pass through the table's policy before they are written. In
   `masked` and `none` mode raw values are never stored; in `raw` mode only if
   `store_raw_samples=True` (default False). A masked column gets a `PII` fact (MEASURED, with the
   detector's name), which agents see as "PII email".
2. **In evidence.** FactLang evidence keeps the SQL only as a template, with literals blanked.
3. **In errors.** Messages from probes and drivers are masked before they are stored or shown;
   in agent tool and server error messages every quoted value becomes `?` unless the policy is `raw`.
4. **In rendered views.** Every profile ends with a final outbound sweep.
5. **In drafting payloads.** Values one at a time, comments as free text, then a sweep over the
   whole payload (`pii_sweep_hits` counts what it caught).
6. **In every agent tool result.** Text and structured fields alike go through the policy of the
   tables they touch, then the sweep.

## Practical advice

- Leave the default (`masked`) on, and opt specific *tables* into `raw` when their values help the
  agent and are not personal (product catalogues, status code tables).
- Use `none` for anything regulated.
- The LLM used for drafting and the agent's own model both see only what this policy allows.
- See [[Security]] for what SchemaGuru stores and what it never does.

## See also

- [[Security]] - the wider security model
- [[Sampling]] - sampling of *measurement*, a different topic
- [[Extending-Detector]] - add a detector
- [[Drafting]] - what the LLM is sent
