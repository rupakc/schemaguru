# Token counter

Views are fitted to a token budget; the counter decides what a token is.

## Protocol

```py
class TokenCounter(Protocol):
    def count(self, text: str) -> int: ...
```

Shipped: `CharTokenCounter` (default, `ceil(len(text) / 4)`) and `TiktokenCounter`
(extra `tiktoken`; select it with `Config(token_counter="tiktoken")`). The count must be
deterministic and monotone enough that a longer text never counts fewer tokens than its
prefix — the budget fit relies on it.

## Worked example: a conservative word counter

```python
import math

import schemaguru as sg


class WordCounter:
    """Over-estimates: 1.4 tokens per whitespace-separated word, plus one per line."""

    def count(self, text: str) -> int:
        return math.ceil(1.4 * len(text.split())) + text.count("\n")


import duckdb

from schemaguru.catalog import DBAPIExecutor, reader_for

conn = duckdb.connect("warehouse.duckdb", read_only=True)
guru = sg.Guru(reader=reader_for("duckdb", DBAPIExecutor(conn)), dialect="duckdb",
               token_counter=WordCounter())
guru.crawl()
view = guru.context("orders by status", budget_tokens=300)
print(view.tokens, "tokens by WordCounter;", [t.table for t in view.tables])
guru.close()
conn.close()
```

`Guru(token_counter=...)` takes precedence over `Config.token_counter`.

## Test template

```py
def test_counter_contract():
    c = MyCounter()
    assert c.count("") == 0
    assert c.count("a b c") <= c.count("a b c d")
    assert c.count("x" * 1000) == c.count("x" * 1000)
```
