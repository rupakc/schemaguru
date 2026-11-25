# PII and value masking

SchemaGuru shows an agent (and a drafting LLM) a few *values* — the observed values of
low-cardinality columns, a handful of examples for others — because "status takes `paid`,
`PAID`, `refunded`" is knowledge names cannot give. Personal data in those values is masked
by default.

## Three modes

| `SamplingConfig.llm_values` | What leaves SchemaGuru | Default |
|---|---|---|
| `masked` | values, with anything detected as personal replaced by its shape: `<email>`, `<phone>`, `<name>`, `<iban>`, `<card>`, `<ip>`, `<uuid>`, `<secret>`, `<address>`, `<hex32>` (hex of that length), `<free_text len=…>` | **yes** |
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
(checksum), card (Luhn), IPv4/IPv6, UUID, long hex, JWT- and secret-like high-entropy strings,
a person-name heuristic, a postal-address heuristic, and free text (over 60 characters with
spaces). Blank values count as NULL. Detectors are plug-ins:
[add one](../extending/pii-detector.md).

* **Hex** (`<hexN>`) is 20 or more hex characters with at least one letter `a`–`f`: 16 would
  mask JSON numbers, and an all-digit run is a number, not a hash.
* **Secrets** (`<secret>`): JWTs, published token shapes (`ghp_…`, `github_pat_…`, `xoxb-…`,
  `xapp-…`, `AKIA…`, `sk_live_…`, …) even when glued to a word by `_` or `-`
  (`token_ghp_…`), the value after a credential key (`api_key=…`, `Bearer …`), and
  high-entropy strings. After `password`, `passwd` or `pwd` with `=` or `:` the value is
  masked whatever its length (`password: hunter2` → `password: <secret>`), unless it is a
  column type (`password: varchar(255)` describes a schema). A path or URL
  (`/var/data/x.csv`, `s3://bucket/key`) is a secret only when a piece of it looks like a
  token; a hex or UUID segment is masked in place (`/var/data/<hex32>/x.csv`).
* **Addresses** (`<address>`) are masked as whole values and inside sentences:
  `ship to 42 Elm Street, Springfield today` → `ship to <address> today`.

## The name heuristic, honestly

* Two to four capitalised tokens with at least one hit in the bundled name list are masked
  as `<name>`, joined by spaces or a comma (`Garcia, Maria`, `Garcia,Maria`), with initials
  (`M. Garcia`). This masks some non-names on purpose — product and title terms such as
  "White Paper", "Brown Sugar" or "Price List" — because failing safe is the point.
* Two or more listed names in lower case are masked too (`maria garcia`), and so are some
  lower-case word pairs that happen to be names ("will mark").
* A lone listed surname is masked when it is possessive (`Garcia's order` →
  `<name>'s order`).
* After a person cue — `customer`, `contact`, `by`, `from` — two to four capitalised tokens
  are masked even when no token is in the list (`customer <name>`), and after an honorific
  (`Mr`, `Ms`, `Mrs`, `Dr`, `Prof`, …) one to four are. Prose such as "sorted by Created At"
  or "Data from Google Analytics" is masked as a result.
* It does **not** mask single-token names without a cue (`Jean-Luc`, a first name alone) or
  lower-case names outside the list. If such values are personal, use `none` for that table.
* Quoted identifiers in stored SQL templates are only masked by shape detectors (email,
  phone, IBAN, card, IP, UUID, hex, secret), never by the name heuristic, so a column called
  `"Unit Price"` survives.

Masking reduces exposure; it is not anonymisation. If a table must not leave your
environment in any form, set it to `none` — or keep it out of the crawl with an
[exclude glob](crawl.md#scope-filters).

## Known limits

Masking trades precision for recall: when a rule cannot tell a personal value from an ordinary
one, it masks. What that means in practice:

- Place and brand names that share a word with the name list are masked as names
  (`Austin Store` → `<name>`): a name list cannot tell a town or a brand from a person.
- A dotted quad is an IP address whatever precedes it, so a four-part version number is
  masked (`release 2.10.3.1` → `release <ip>`); three-part and `v`-prefixed versions
  (`1.2.3`, `v10.0.1`) are kept.
- A digit string that starts with `00` and a non-zero digit is the international dialling
  form, so a zero-padded id written that way is masked (`0012345678901` → `<phone>`); ids
  with three or more leading zeros (`000123456`) are kept.
- After `password:`, `passwd:` or `pwd:` any value is masked, a word included
  (`password: required` → `password: <secret>`); only a column type (`varchar(255)`,
  `text`, …) is kept, so a password that is literally a type name would show.
- A path is kept unless a piece of it looks like a token (letters mixed with digits or with
  mixed case, or 16+ high-entropy letters) or it holds a `+` and is high-entropy as a whole: a
  path with a camel-case folder of 8+ letters is masked whole, and a token of fewer than 16
  letters of one case with no digit, inside a path, is not detected.
- Addresses inside sentences need capitalised street names (`42 Elm Street`), except before an
  unambiguous street word (`12 baker street`); a lower-case `12 baker st` inside a sentence is
  not detected.
