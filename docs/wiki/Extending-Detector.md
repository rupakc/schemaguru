# Extending: Detector

> A PII `Detector` recognises one shape of personal value, such as an e-mail address or a loyalty-card number, so masking can replace it with a shape token.
> It answers one question about one value: does the *whole* value have this shape?
> Register it with `schemaguru.pii.register_detector`; it then applies everywhere masking applies (storage, views, drafting payloads, tool results).

## The protocol

```python
class Detector(Protocol):
    name: str          # unique; re-registering a name needs replace=True
    category: str      # the PII category, also the shape token: <category>
    def matches(self, value: str) -> bool: ...
```

Built-ins run first, in priority order (uuid, email, ipv4, ipv6, iban, card, hex, phone, secret,
address, person_name, free_text); plug-ins run after them, and the first match decides. So a
plug-in cannot override a built-in for a value the built-in already matches; to change a
built-in, register a replacement under its name with `replace=True`.
`schemaguru.pii.reset_detectors()` restores the built-in set (useful in tests).

## Worked example

```python
import re
from schemaguru.pii import mask_value, register_detector


class LoyaltyCardDetector:
    """Shop loyalty numbers such as 'LC-0042-7781' are personal: mask them as <loyalty_card>."""

    name = "loyalty_card"
    category = "loyalty_card"            # also the shape token: <loyalty_card>
    _shape = re.compile(r"LC-\d{4}-\d{4}")

    def matches(self, value: str) -> bool:      # does the WHOLE value have this shape?
        return self._shape.fullmatch(value.strip()) is not None


register_detector(LoyaltyCardDetector())
print(mask_value("LC-0042-7781"))
```

Real output (SchemaGuru 0.1.0):

```text
('<loyalty_card>', 'loyalty_card')
```

Keep detectors **strict**: a detector that matches too much masks useful values (status codes,
product names); one that matches too little leaks. Match the whole value; the outbound sweep
(which searches inside free text) uses only the built-in unambiguous shapes.

## Test template

```python
from schemaguru.pii import mask_value, register_detector, reset_detectors

def test_loyalty_card_masked():
    reset_detectors(); register_detector(LoyaltyCardDetector())
    try:
        assert mask_value("LC-0042-7781") == ("<loyalty_card>", "loyalty_card")
        assert mask_value("LC-42") == ("LC-42", None)          # negative control
        assert mask_value("paid") == ("paid", None)
    finally:
        reset_detectors()
```

## See also

- [[PII-and-Sampling-Policy]]
- [[Security]]
- [[Extending-FactKind]]
- [[Contributing]]
