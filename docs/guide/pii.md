# PII and value masking

SchemaGuru shows an agent (and a drafting LLM) a few *values* — the observed values of
low-cardinality columns, a handful of examples for others — because "status takes `paid`,
`PAID`, `refunded`" is knowledge names cannot give. Personal data in those values is masked
by default.

## Three modes

| `SamplingConfig.llm_values` | What leaves SchemaGuru | Default |
|---|---|---|
| `masked` | values, with anything detected as personal replaced by its shape: `<email>`, `<phone>`, `<name>`, `<iban>`, `<card>`, `<ip>`, `<uuid>`, `<hex32>`, `<free_text len=…>` | **yes** |
| `raw` | values unmasked — only on your explicit opt-in | no |
| `none` | no values at all: names, types, comments and statistics only | no |

`masked` masks values that **look** personal. A value with no personal shape — an enum
code, a filter literal, the maximum of a timestamp column — is shown as it is. If a table's
values must not leave in any form, use `none`: it hides every value and keeps only names,
types, comments and counts.

Override per table glob with `per_glob` — the narrowest matching glob wins, and between
equally narrow globs the most restrictive mode wins, so an ambiguous config never widens
access:

```python
import schemaguru as sg
from schemaguru.pii import ValuePolicy

cfg = sg.SamplingConfig(per_glob={"shop.*": "masked", "shop.customers": "none"})
for t in ("orders", "customers"):
    print(t, ValuePolicy.for_table(sg.ObjectRef(None, "shop", t), cfg))
```

Globs match **case-insensitively**: `hr.*` covers Snowflake's upper-cased
`DB.HR.EMPLOYEES` as well as `hr.employees`, because a case-sensitive miss would fail open
(the table would fall back to the default mode). A glob that matches no stored table is
reported by `guru.load_overlays()` and `schemaguru lint` as warning `SG507`: a typo there
would otherwise leave the tables it meant to restrict on the default mode.

Raw samples are written to the store only in `raw` mode **and** with
`store_raw_samples=True` (default `False`).

## Where masking happens

Values are masked at every boundary, not once:

* **at storage** — measured values are stored masked (unless raw is opted into twice, above);
* **in evidence** — SQL is stored as a template only, every literal blanked (`?`);
* **in errors** — exception messages are masked before they are recorded or returned;
* **in rendered views** — a final shape sweep runs over every view;
* **in every agent tool result**, and over every payload sent to a drafting LLM (hits are
  counted, `pii_sweep_hits`).

## Detection: shape first, never name alone

A column is not classified by its name. A frequency enum whose name contains "email" is not
an email column; an anonymised identifier that looks like an enum by cardinality is flagged
for review rather than auto-classified. Detectors look at values:

```python
from schemaguru.pii import mask_value

for v in ["ann.lee@example.com", "+44 20 7946 0958", "DE89 3704 0044 0532 0130 00",
          "4111 1111 1111 1111", "192.0.2.17", "Ann Lee", "White Paper", "paid"]:
    print(repr(v), "->", mask_value(v))
```

Shipped detectors: email (including lists), phone (E.164 and national forms), IBAN
(checksum), card (Luhn), IPv4/IPv6, UUID, long hex (≥ 20 characters; 16 would mask JSON
numbers), JWT- and secret-like high-entropy strings, a person-name heuristic, a postal-address
heuristic, and free text (over 60 characters with spaces). Blank values count as NULL.
Detectors are plug-ins: [add one](../extending/pii-detector.md).

## The name heuristic, honestly

* Two capitalised tokens with at least one hit in the bundled name list are masked as
  `<name>`. This masks some non-names on purpose — product and title terms such as
  "White Paper", "Brown Sugar" or "Price List" — because failing safe is the point.
* It does **not** mask lowercase names, hyphenated names or single-token names. If such
  values are personal, use `none` for that table.
* Quoted identifiers in stored SQL templates are only masked by shape detectors (email,
  phone, IBAN, card, IP, UUID, hex, secret), never by the name heuristic, so a column called
  `"Unit Price"` survives.

Masking reduces exposure; it is not anonymisation. If a table must not leave your
environment in any form, set it to `none` — or keep it out of the crawl with an
[exclude glob](crawl.md#scope-filters).
