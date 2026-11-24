`WHERE status = 'paid'` counts **5,000** orders; **5,412** are paid.
The overlay declares `paid, refunded, cancelled`; measurement found `PAID` as well.
SchemaGuru keeps the human expectation and states the contradiction (here under a
waiver with an expiry date):

```text
  ⚠ WAIVED until 2027-01-31 (legacy upper-case rows being migrated): status: declared values 'paid','refunded','cancelled' — observed also 'PAID' (412 rows)
  ⚠ status: case drift: 'paid'/'PAID' differ only in case — compare case-insensitively
    status       VARCHAR  values: 'paid','refunded','cancelled' (+ observed 'PAID'); distinct 4 (measured 2026-11-24); Payment state of the order.
```
