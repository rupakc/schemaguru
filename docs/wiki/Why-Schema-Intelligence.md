# Why Schema Intelligence

> An agent that queries a warehouse needs facts the table names do not carry: which joins really match, which values a column takes, which rows are deleted.
> Without them it guesses, and a wrong guess returns a believable wrong number instead of an error.
> Schema intelligence is that missing knowledge, measured where possible, labelled with its source, and served in small doses.

## The wrong join

Take the shop. A question: "how many purchase events per order?" There is an `events` table with a
`session_id` column and an `orders` table with an `order_id` column. Both are keys, both look like
identifiers, and an agent that sees only names will happily write

```sql
SELECT o.order_id, count(*) FROM shop.events e JOIN shop.orders o ON ... GROUP BY 1
```

with *some* join condition. In the shop fixture, `events.session_id -> orders.order_id` matches
**0%** of keys (one is text like `s42`, the other an integer). Another tempting route goes through
`accounts`: both `events` and `orders` carry an `account_id`. That join matches 100% on both sides,
so the query runs and returns numbers. But every account has on average 250 orders and 333 events,
so joining events to orders through the account multiplies rows by tens of thousands. Nothing
errors. The answer is just wrong.

SchemaGuru measures both joins and refuses the second route by its *shape*. This is the real
output of `guru.find_path("shop.events", "shop.orders")` on the shop fixture (generated with
SchemaGuru 0.1.0 on 2026-11-24, measurements from the test suite's stand-in backend):

```text
every route shop.events -> shop.orders pivots through hub shop.accounts on two attribution edges (fan-in > 10): a cartesian hazard
```

That refusal is a fact the agent can act on ("there is no safe route; ask the user, or use
sessions"), instead of a number it cannot check.

## What goes wrong without it

These are the failure classes SchemaGuru is built around. Each one produced a measured wrong
answer in the project SchemaGuru's ideas come from (see [[Design-Decisions]]):

| Failure | What the agent does | What SchemaGuru tells it |
|---|---|---|
| Thin join treated as broken (or the other way round) | drops a correct join, or trusts a broken one | the join's measured match rate *and* reverse coverage |
| Join direction ignored | assumes "every payment has an order" means "every order has a payment" | forward match 100%, reverse coverage 5% |
| Hub cartesian | joins two child tables through a shared parent | a typed refusal `HUB_PIVOT` |
| Enum case drift | filters `status = 'paid'` and misses 412 `PAID` rows | "observed also 'PAID' (412 rows) - compare case-insensitively" |
| Dates stored as text | compares a string with a date | "stored as VARCHAR, holds ISO-8601 date text - CAST before comparing" |
| Soft-delete flag that is TRUE or NULL | filters `deleted = FALSE` and gets zero rows | "values: TRUE (300); null 97%" next to the live-rows predicate |
| Role guessed from a column name | treats a hashed enum as personal data, or the reverse | detection by value shape and cardinality, never by name alone |
| Stale prose | trusts a description written two years ago | every fact carries its source and date; refresh reports drift |

## Why not just put the schema in the prompt?

Three reasons:

1. **It does not fit.** A real warehouse has thousands to hundreds of thousands of tables.
   SchemaGuru scopes: it picks the handful of tables a question is about (plus the tables that
   connect them) and fits them to a token budget. See [[Scoping]].
2. **Names are not knowledge.** `DESCRIBE TABLE` shows column names and types. It does not show
   that a join matches 5% backwards, that `status` has a fourth value, or that `deleted` is never
   FALSE. Those come from measurement. See [[Measurement]].
3. **Unlabelled knowledge is dangerous.** A sentence in a prompt looks equally true whether a
   person wrote it last week, an LLM guessed it, or a probe measured it this morning. SchemaGuru
   labels every line. See [[Concepts]] and [[Rendering]].

## What SchemaGuru is not

- It does not write SQL for the agent (no natural-language-to-SQL).
- It does not run the agent's SQL. Running queries is the host's job; an optional read-only guard
  helper is provided ([[Feedback-and-Guard]]).
- It never writes to the warehouse. Metadata reads and read-only, budgeted measurement probes are
  all it does.
- It does not need an LLM. Discovery, measurement, the join graph, scoping and rendering work with
  zero LLM calls; an LLM only adds drafted descriptions ([[Drafting]]).

## See also

- [[Concepts]] - facts, provenance, status and precedence
- [[Path-Search-and-Hub-Guard]] - how the refusal above is computed
- [[Design-Decisions]] - the rules and the lessons behind them
- [[Home]]
