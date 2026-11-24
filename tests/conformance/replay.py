"""Replay harness for catalog readers (spec 6.2): recorded system-view rows, no live engine.

``ReplayExecutor`` maps ``(normalised SQL fingerprint, params)`` to recorded rows. Unknown SQL
raises ``UnknownSQL`` carrying the fingerprint, so a changed reader query fails loudly instead of
silently returning nothing. ``RecordingExecutor`` wraps a real executor to produce a fixture.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from schemaguru.catalog.executor import MetadataExecutor
from schemaguru.catalog.protocol import CatalogReader

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "catalog"


def fixture_doc(dialect: str) -> dict[str, Any]:
    doc: dict[str, Any] = json.loads(
        (FIXTURES / dialect / "queries.json").read_text(encoding="utf-8")
    )
    return doc


class UnknownSQL(AssertionError):
    def __init__(self, fingerprint: str, params: Sequence[Any]) -> None:
        super().__init__(
            f"no recorded rows for this SQL; re-record the fixture if the reader query changed.\n"
            f"fingerprint: {fingerprint}\nparams: {list(params)!r}"
        )
        self.fingerprint = fingerprint


def fingerprint(sql: str) -> str:
    """Whitespace-collapsed, lower-cased SQL."""
    return re.sub(r"\s+", " ", sql).strip().lower()


def encode(value: Any) -> Any:
    """JSON-safe form of a cell or parameter (datetimes, decimals, bytes are tagged)."""
    if isinstance(value, datetime):
        return {"$datetime": value.isoformat()}
    if isinstance(value, date):
        return {"$date": value.isoformat()}
    if isinstance(value, Decimal):
        return {"$decimal": str(value)}
    if isinstance(value, (bytes, bytearray)):
        return {"$bytes": bytes(value).hex()}
    if isinstance(value, (list, tuple)):
        return [encode(v) for v in value]
    if isinstance(value, Mapping):
        return {str(k): encode(v) for k, v in value.items()}
    return value


def decode(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value) == {"$datetime"}:
            return datetime.fromisoformat(value["$datetime"])
        if set(value) == {"$date"}:
            return date.fromisoformat(value["$date"])
        if set(value) == {"$decimal"}:
            return Decimal(value["$decimal"])
        if set(value) == {"$bytes"}:
            return bytes.fromhex(value["$bytes"])
        return {k: decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [decode(v) for v in value]
    return value


def _key(sql: str, params: Sequence[Any]) -> str:
    return fingerprint(sql) + "\x00" + json.dumps(encode(list(params)), sort_keys=True)


class ReplayExecutor:
    def __init__(self, queries: Sequence[Mapping[str, Any]]) -> None:
        self._rows: dict[str, list[dict[str, Any]]] = {}
        for q in queries:
            key = fingerprint(q["sql"]) + "\x00" + json.dumps(q["params"], sort_keys=True)
            self._rows[key] = q["rows"]
        self.seen: list[str] = []

    def assert_all_consumed(self) -> None:
        """Every recorded query was used; a leftover means the fixture is stale."""
        stale = sorted(set(self._rows) - set(self.seen))
        assert not stale, f"{len(stale)} recorded queries never ran (stale fixture): " + "; ".join(
            k.split("\x00")[0][:100] for k in stale[:5]
        )

    @classmethod
    def from_fixture(cls, dialect: str) -> ReplayExecutor:
        return cls(fixture_doc(dialect)["queries"])

    def run(
        self, sql: str, params: Sequence[Any] = (), *, timeout_s: float | None = None
    ) -> list[dict[str, Any]]:
        key = _key(sql, params)
        self.seen.append(key)
        if key not in self._rows:
            raise UnknownSQL(fingerprint(sql), params)
        return [{k: decode(v) for k, v in row.items()} for row in self._rows[key]]


class RecordingExecutor:
    """Wraps a real executor and remembers every query it ran (see the fixtures README)."""

    def __init__(self, inner: MetadataExecutor) -> None:
        self._inner = inner
        self.queries: list[dict[str, Any]] = []

    def run(
        self, sql: str, params: Sequence[Any] = (), *, timeout_s: float | None = None
    ) -> list[dict[str, Any]]:
        rows = self._inner.run(sql, params, timeout_s=timeout_s)
        self.queries.append(
            {
                "sql": fingerprint(sql),
                "params": encode(list(params)),
                "rows": [{k: encode(v) for k, v in r.items()} for r in rows],
            }
        )
        return rows


def read_all(
    reader: CatalogReader, *, only_catalogs: Sequence[str] | None = None
) -> dict[str, Any]:
    """Walk a reader exactly as a crawl does and return a canonical, sorted dict."""
    from schemaguru.model.refs import ObjectRef

    schemas: list[Any] = []
    tables: list[Any] = []
    for catalog in _catalogs(reader):
        if only_catalogs is not None and catalog not in only_catalogs:
            continue
        token: str | None = None
        while True:
            page = reader.list_schemas(catalog, page_token=token)
            schemas += page.items
            token = page.next_token
            if token is None:
                break
    for schema in schemas:
        token = None
        while True:
            tpage = reader.list_tables(schema, page_token=token, changed_since=None)
            tables += tpage.items
            token = tpage.next_token
            if token is None:
                break
    refs: list[ObjectRef] = [t.ref for t in tables]
    columns: list[Any] = []
    constraints: list[Any] = []
    for i in range(0, len(refs), 200):
        batch = refs[i : i + 200]
        for cols in reader.columns(batch).values():
            columns += cols
        for cons in reader.constraints(batch).values():
            constraints += cons
    return {
        "schemas": [s.to_dict() for s in sorted(schemas, key=lambda s: s.key())],
        "tables": [t.to_dict() for t in sorted(tables, key=lambda t: t.ref.canonical())],
        "columns": [
            c.to_dict() for c in sorted(columns, key=lambda c: (c.ref.canonical(), c.ordinal))
        ],
        "constraints": [
            c.to_dict()
            for c in sorted(
                constraints, key=lambda c: (c.table.canonical(), c.kind, list(c.columns))
            )
        ],
    }


def _catalogs(reader: CatalogReader) -> list[str | None]:
    found: list[str | None] = list(reader.list_catalogs())
    return found or [None]


def record(
    reader_factory: Any,
    inner: MetadataExecutor,
    dialect: str,
    *,
    only_catalogs: Sequence[str] | None = None,
) -> None:
    """Write ``queries.json`` and ``expected.json`` for ``dialect`` from a live executor.

    Review ``expected.json`` by hand against the engine before committing it.
    """
    rec = RecordingExecutor(inner)
    out = read_all(reader_factory(rec), only_catalogs=only_catalogs)
    target = FIXTURES / dialect
    target.mkdir(parents=True, exist_ok=True)
    doc = {"dialect": dialect, "only_catalogs": only_catalogs, "queries": rec.queries}
    (target / "queries.json").write_text(
        json.dumps(doc, indent=1, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (target / "expected.json").write_text(
        json.dumps(out, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
