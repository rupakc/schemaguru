# Extending: TokenCounter

> A `TokenCounter` tells the budget fitter how many tokens a piece of text costs, so views fit the model you actually use.
> The default is `len(text) / 4` rounded up; `Config(token_counter="tiktoken")` uses tiktoken (extra), and any object with a `count(text) -> int` method can be passed in.
> Pass it as `sg.Guru(..., token_counter=...)`; an explicit counter survives config changes.

## The protocol

```python
class TokenCounter(Protocol):
    def count(self, text: str) -> int: ...
```

Shipped: `CharTokenCounter` (the default heuristic) and `TiktokenCounter(encoding="cl100k_base")`
(extra `tiktoken`), both in `schemaguru.scope`.

## Worked example

```python
class WordCounter:
    """Count tokens as words x 1.3: closer than chars/4 for some models' tokenizers."""

    def count(self, text: str) -> int:
        return math.ceil(len(text.split()) * 1.3)

guru = sg.Guru(reader=reader, store="shop.db", dialect="duckdb", token_counter=WordCounter())
```

Verified on the shop fixture: views render and `view.tokens` is counted by the custom counter.
A counter should be deterministic (same text, same count) or views stop being byte-stable, and
it should not undercount: the fitter trusts it to keep a view inside `budget_tokens`.

## Test template

```python
def test_counter_is_deterministic_and_monotone():
    c = WordCounter()
    assert c.count("a b c") == c.count("a b c")
    assert c.count("a b c d e f") >= c.count("a b c")

def test_view_fits_budget(guru):
    assert guru.context("orders", budget_tokens=300).tokens <= 300
```

## See also

- [[Scoping]]
- [[Rendering]]
- [[Configuration]]
- [[Extending-Retriever-and-Embedder]]
