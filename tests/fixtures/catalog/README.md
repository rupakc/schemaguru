# Recorded catalog fixtures

`tests/conformance/test_readers.py` replays each reader against rows recorded from a real engine.
A fixture directory `<dialect>/` (the FactLang dialect name) holds:

- `queries.json`: every metadata query the reader ran, as `{sql (whitespace-collapsed,
  lower-cased), params, rows}`. Datetimes, decimals and bytes are tagged (`{"$datetime": ...}`).
- `expected.json`: the `Schema/Table/Column/Constraint` output the reader must produce.

Unknown SQL fails the test and prints its fingerprint, so editing a reader query means
re-recording. Never hand-edit `queries.json` to make a test pass.

## Recording

1. Start the engine and seed a schema with at least two schemas, five tables, a view, a composite
   primary key, a foreign key (where the engine has them), a comment, and a quoted or odd
   identifier. Use generic shop names only.
2. Run:

   ```python
   from tests.conformance.replay import record

   record(MyReader, DBAPIExecutor(conn), "mydialect")
   ```

   (`python -m tests.conformance.record <dialect>` does this for the engines with a seeded
   recorder.)
3. Review `expected.json` against the engine by eye (types, nullability, comments, FK targets);
   it is a contract, not a snapshot to accept blindly.
4. Strip anything environment-specific (hostnames, user names, timestamps that vary) from the
   rows before committing; the recorded rows are public.

The `integration` CI job runs the same assertions against live containers
(`SCHEMAGURU_TEST_<ENGINE>_DSN`), see `tests/integration/catalog/`.

`view_definitions` entries in the `mysql`, `clickhouse`, `tsql` and `trino` fixtures are
synthetic (`"synthetic_view_definitions": true` in `queries.json`): written in the engine's
documented shape for the seeded `v_customers` view because the containers were not available when
the reader grew `view_definitions`. Re-run `python -m tests.conformance.record <engine>` against
the live engine to replace them; the flag then disappears.
