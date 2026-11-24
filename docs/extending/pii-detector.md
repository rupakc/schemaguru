# PII detector

Detectors decide which values are personal and get masked to a shape. Add one for an
identifier format your organisation uses.

## Protocol

```py
class Detector(Protocol):
    name: str          # unique; shown as the PII fact's detector
    category: str      # email, phone, name, address, gov_id, card, iban, ip, free_text, other
    def matches(self, value: str) -> bool: ...
```

`matches` sees one whole value (strip it yourself if whitespace matters) and must be fast and
safe on hostile input — the built-ins are tested against 50,000-character adversarial strings.
Register with `sg.pii.register_detector(d)` (a duplicate name raises unless `replace=True`);
`sg.pii.reset_detectors()` restores the built-in set. Plug-ins run after the built-ins.

## Worked example: customer numbers

```python
import re

import schemaguru as sg
from schemaguru.pii import classify_column, mask_value, register_detector


class CustomerNumber:
    name = "customer_number"
    category = "gov_id"
    _rx = re.compile(r"CUS-\d{6}-[A-Z]")

    def matches(self, value: str) -> bool:
        return self._rx.fullmatch(value.strip()) is not None


register_detector(CustomerNumber())
print(mask_value("CUS-104233-K"))
print(classify_column([("CUS-104233-K", 3), ("CUS-551100-B", 2)], distinct=2, name="ref"))
```

When a measured column's observed values match, the column gets a `PII` fact (`MEASURED`,
`detector=customer_number`), its values are masked everywhere they leave SchemaGuru, and the
agent is told the column holds personal data. Detection is by value shape; a column name
alone never classifies a column.

## Test template

```py
import pytest

@pytest.mark.parametrize("value", ["CUS-104233-K", " CUS-000001-A "])
def test_matches(value):
    assert CustomerNumber().matches(value)

@pytest.mark.parametrize("value", ["CUS-10423-K", "cus-104233-k", "", "x" * 50_000])
def test_rejects(value):
    assert not CustomerNumber().matches(value)
```
