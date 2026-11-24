# Shop fixture: traps and exact answers

Schema `shop`, built by `build_shop` (seed `random.Random(7)`); every number below is exposed
by `ShopFacts`. Rates are by DISTINCT KEYS unless "row-level".

| Trap | Where | Exact answer |
|---|---|---|
| PII column | `customers.email` | 1,000 distinct `user{n}@example.com` |
| Enum | `customers.segment` | retail 334, wholesale 333, vip 333 |
| Soft-delete TRUE/NULL | `orders.deleted` | TRUE 300, NULL 9,700, FALSE 0 |
| STRING dates | `orders.created_at` | VARCHAR ISO-8601 `...Z` |
| Enum case drift | `orders.status` | paid 5000, refunded 2588, cancelled 2000, `PAID` 412 |
| Orphans | `orders.customer_id -> customers` | 20 orphan rows with 20 distinct keys; row-level 9,980/10,000 = 0.998; key-level 1,000/1,020; coverage 1.0 |
| Composite PK | `order_items (order_id, line_no)` | 20,000 rows, 2 lines per order |
| Thin but correct | `payments.order_id -> orders` | 500 keys, match 1.0, coverage 0.05 |
| No direct join | `events.session_id -> orders.order_id` | match 0.0 (string `s{n}` vs integer) |
| Real join | `events.session_id -> sessions` | match 1.0 |
| Hub / cartesian | `accounts` (100 rows) | `orders.account_id` fan-in 40, `events.account_id` fan-in 60, both match 1.0 |
| Asymmetric | `ref_child.parent_id -> ref_parent.id` | forward 1.0, coverage 3/1000 = 0.003 |
| Broken | `broken_child.parent_id -> ref_parent.id` | match 10/1000 = 0.01 |
| Empty table | `empty_t` | 0 rows; null rate unknown (`None`) |
| All-null column | `all_null_t.c` | 100 rows, null rate 1.0 |
| Anon-looking enum | `anon_enum_t.kind` | 10,000 rows, 5 distinct `<32 hex>@anon.example` |
| Sampling | `big_t` | 2,000,000 rows (`build_big`, lazily; fixture `shop_duckdb_big`) |
| Nulls | `customers.phone` | 100 NULL (every 10th), rate 0.1 |
