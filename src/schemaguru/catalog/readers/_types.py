"""Native type text -> FactLang type family (``integer, decimal, float, string, boolean, date,
timestamp, time, bytes, json, array, struct, other``). Local, ordered regex table."""

from __future__ import annotations

import re

_RULES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(p, re.IGNORECASE), fam)
    for p, fam in (
        (r".*\[\]$|^array\b|^_", "array"),
        (r"^(struct|row|record|composite)\b|^map\b", "struct"),
        (
            r"^(tinyint|smallint|int|integer|bigint|hugeint|utinyint|usmallint|uinteger|ubigint"
            r"|int2|int4|int8|serial|bigserial|smallserial|oid)\b",
            "integer",
        ),
        (r"^(numeric|decimal|money)\b", "decimal"),
        (r"^(real|float|float4|float8|double)\b", "float"),
        (r"^(bool|boolean)\b", "boolean"),
        (r"^timestamp|^datetime", "timestamp"),
        (r"^time\b|^time\(|^time with|^time without", "time"),
        (r"^date\b", "date"),
        (r"^(json|jsonb)\b", "json"),
        (r"^(bytea|blob|binary|varbinary|bit)\b", "bytes"),
        (r"^(char|character|varchar|text|string|name|bpchar|citext|uuid|enum)\b", "string"),
    )
)


def family_of(native_type: str) -> str:
    text = native_type.strip()
    for pattern, family in _RULES:
        if pattern.search(text):
            return family
    return "other"
