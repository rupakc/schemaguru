from __future__ import annotations

from schemaguru.scope.tokens import CharTokenCounter, TokenCounter


def test_char_counter_is_ceil_len_over_4() -> None:
    c: TokenCounter = CharTokenCounter()
    assert [c.count(s) for s in ("", "a", "abcd", "abcde", "x" * 401)] == [0, 1, 1, 2, 101]
