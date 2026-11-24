# Diagnostic codes

Generated from `schemaguru.diagnostics.CODES`; a test asserts the two agree.

| Code | Meaning |
|---|---|
| SG201 | overlay 'version' is missing or unsupported |
| SG202 | overlay has an unknown key (a close match is suggested) |
| SG203 | overlay reference does not resolve to anything in the store |
| SG204 | overlay reference is ambiguous; the candidates are listed |
| SG205 | overlay filter text is invalid |
| SG206 | overlay waiver has no valid 'expires' date and is ignored |
| SG207 | overlay waiver has expired and is ignored |
| SG208 | overlay file is unreadable or an entry is malformed |
| SG209 | dbt manifest is unreadable |
| SG701 | SQLite FTS5 is unavailable; search falls back to a slower LIKE scan |
| SG702 | the store was written by a newer schema version than this library supports |
