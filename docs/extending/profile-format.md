# Render profile and format

A **profile** decides how one table is written for one kind of reader; a **format** decides
how a whole view is serialised. Both are registries.

## Protocols

```py
class Profile(Protocol):
    def render_table(self, eff: TableKnowledge, level: DetailLevel) -> str: ...

Formatter = Callable[[ContextView], str]
```

`TableKnowledge` carries one table's structure (`ref`, `table`, `columns`, `constraints`) and
its effective knowledge (`eff.get(kind, column=None)` returns the resolved `Effective` for a
kind), plus `now` and `config`. `level` is the detail level the budget fit asks for
(`FULL`, `STANDARD`, `LITE`, `NAME_ONLY`); the fit tries richer levels first and keeps the
first that fits, so a lower level must never be longer than a higher one.

Register with `sg.render.register_profile(name, profile)` and
`sg.render.register_format(name, fn)`; use them as `guru.context(q, profile=name)` and
`guru.context(q, format=name)` / `str(view)`.

!!! warning "Keep the outbound sweep"
    The built-in profiles end with `schemaguru.render.profiles.finish(eff, text)`, which runs
    the value policy's shape sweep. A custom profile that shows values must do the same.

## Worked example: a one-line-per-table profile and a YAML format

```python
import yaml

import schemaguru as sg
from schemaguru.model.detail import DetailLevel
from schemaguru.render import register_format, register_profile
from schemaguru.render.profiles import finish


class OneLine:
    def render_table(self, eff, level):
        if level is DetailLevel.NAME_ONLY:
            return eff.ref.canonical()
        rows = eff.get("ROW_COUNT")
        n = rows.primary.value.get("rows") if rows is not None and rows.primary else None
        cols = ", ".join(c.ref.column for c in eff.columns)
        return finish(eff, f"{eff.ref.canonical()} ({'?' if n is None else n} rows): {cols}")


def to_yaml(view):
    return yaml.safe_dump({"question": view.question, "fingerprint": view.fingerprint,
                           "blocks": [b.text for b in view.blocks]}, sort_keys=True)


register_profile("oneline", OneLine())
register_format("yaml", to_yaml)

guru = sg.Guru.connect("duckdb:///warehouse.duckdb")
guru.measure("shop.payments")
print(guru.context("payments per order", profile="oneline").text)
print(str(guru.context("payments per order", format="yaml"))[:160])
guru.close()
```

## Test template

```py
def test_profile_levels_shrink(shop_guru):
    texts = [shop_guru.context("payments", profile="oneline", budget_tokens=b).text
             for b in (4000, 400, 120)]
    assert len(texts[0]) >= len(texts[1]) >= len(texts[2])
    assert texts[0] == shop_guru.context("payments", profile="oneline").text   # deterministic
```
