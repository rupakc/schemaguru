# Extending: Profile and Format

> A *profile* decides how one table block is rendered for a role; a *format* decides how a whole view is serialised.
> Register a profile with `sg.render.register_profile(name, profile)` and use it with `guru.context(q, profile=name)`; register a format with `sg.render.register_format(name, fn)` and use `format=name`.
> A custom profile must end with the outbound PII sweep, which the helper `finish` applies.

## The protocols

```python
class Profile(Protocol):
    def render_table(self, eff: TableKnowledge, level: DetailLevel) -> str: ...

Formatter = Callable[[ContextView], str]
```

`TableKnowledge` (`schemaguru.render.knowledge`) holds one table's structure (`ref`, `table`,
`columns`, `constraints`) and its resolved knowledge (`get(kind, column=None)` returns the
`Effective` entry, `joins()` the joins touching the table), plus `now` and `config`.
`level` is the detail level the budget fitter chose (`FULL`, `STANDARD`, `LITE`, `NAME_ONLY`);
respect it, because the fitter measures the size of what you return at each level.

Rules: put warnings before confirmations; never print a number without its provenance; and pass
your text through `schemaguru.render.profiles.finish(eff, text)`, the outbound sweep under the
table's value policy. Skipping `finish` can leak values.

## Worked example

```python
import schemaguru as sg
from schemaguru.render import register_format, register_profile
from schemaguru.render.profiles import finish


class ColumnsOnlyProfile:
    """One line per table: its name and column names. For a cheap first routing step."""

    def render_table(self, eff, level) -> str:
        cols = ", ".join(c.ref.column or "" for c in eff.columns)
        return finish(eff, f"{eff.ref.canonical()}({cols})")   # finish = the outbound PII sweep


register_profile("columns", ColumnsOnlyProfile())
register_format("plain", lambda view: f"# {view.question}\n{view.text}")
```

Real output of `str(guru.context("orders and customers", profile="columns", format="plain"))` on
the measured shop fixture (2026-11-24):

```text
# orders and customers
shop.orders(order_id, customer_id, account_id, status, created_at, deleted)

shop.customers(customer_id, email, phone, segment)

shop.order_items(order_id, line_no, product_id)

shop.payments(payment_id, order_id, amount)

Join paths (join keys, advisory; support = the weakest hop's match or coverage):
  shop.orders → shop.customers · support 98.04%
    orders.customer_id = customers.customer_id (N:1)
  shop.orders → shop.order_items · support 100%
    order_items.order_id = orders.order_id (N:1)
  shop.orders → shop.payments · support 5%
    payments.order_id = orders.order_id (1:1)

Not shown: no other candidate tables.
```

The profile controls the table blocks; the view still adds the join paths and the closing line.
`sg.render.profile_names()` lists registered profiles.

## Test template

```python
def test_columns_profile_is_deterministic(guru):
    a = guru.context("orders", profile="columns").text
    b = guru.context("orders", profile="columns").text
    assert a == b and "shop.orders(" in a

def test_format_registered_once():
    with pytest.raises(ValueError):
        register_format("plain", lambda v: v.text)          # already registered
```

## See also

- [[Rendering]]
- [[PII-and-Sampling-Policy]]
- [[Extending-FactKind]]
- [[Agent-Integration]]
