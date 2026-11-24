An agent asked for *events per order* joins `orders → accounts → events` on
`account_id`. Both joins are "correct" (match rate 100%), yet the result has
**3,335,000 rows for 10,000 orders** — 333.5× — because
both tables attribute many rows to one account. No error, a plausible number.

SchemaGuru measured both joins, saw `accounts` receiving two attribution edges, and
refuses the route (`find_join_path` / `schemaguru path shop.orders shop.events`):

```text
every route shop.orders -> shop.events pivots through hub shop.accounts on two attribution edges (fan-in > 10): a cartesian hazard
```

A safe route is returned with its keys and its weakest support:

```text
  shop.payments → shop.orders → shop.customers · support 98.04%
    payments.order_id = orders.order_id (1:1)
    orders.customer_id = customers.customer_id (N:1)
```
