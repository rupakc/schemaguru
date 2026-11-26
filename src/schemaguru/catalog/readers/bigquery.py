"""BigQuery reader over ``INFORMATION_SCHEMA`` (spec 6.2).

Catalog = project, schema = dataset. Region views (``SCHEMATA``, ``TABLE_STORAGE``) are addressed
as ``region-<r>.INFORMATION_SCHEMA``; everything else is dataset-scoped, so table, column and
constraint reads are batched **per dataset** (one query per dataset per call). Column
descriptions come from ``COLUMN_FIELD_PATHS``, table descriptions from ``TABLE_OPTIONS``.

Cost: every INFORMATION_SCHEMA query is billed (minimum 10 MiB). The reader exposes
``bytes_billed`` (read by the crawl into ``CrawlReport``): the sum of the executor's
``last_bytes_billed`` after each query when the executor offers it, else the 10 MiB minimum per
query -- then ``bytes_billed_is_lower_bound`` is true.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime
from typing import Any

from schemaguru.catalog.executor import MetadataExecutor
from schemaguru.catalog.protocol import CatalogError, Page, ReaderCapabilities
from schemaguru.catalog.readers._common import (
    Row,
    group_by_scope,
    lower_keys,
    opt_int,
    to_utc,
    yes_no,
)
from schemaguru.catalog.readers._types import family_of
from schemaguru.catalog.registry import register_reader
from schemaguru.model.refs import ObjectRef
from schemaguru.model.structure import Column, Constraint, ConstraintKind, Schema, Table, TableKind

MIN_BILLED_BYTES = 10 * 1024 * 1024

_KINDS: Mapping[str, TableKind] = {
    "BASE TABLE": "table",
    "CLONE": "table",
    "SNAPSHOT": "table",
    "VIEW": "view",
    "MATERIALIZED VIEW": "materialized_view",
    "EXTERNAL": "external",
}
_CONSTRAINTS: Mapping[str, ConstraintKind] = {
    "PRIMARY KEY": "primary_key",
    "FOREIGN KEY": "foreign_key",
    "UNIQUE": "unique",
}
_PROJECT = re.compile(r"[A-Za-z0-9_\-.:]+\Z")
_DATASET = re.compile(r"[A-Za-z0-9_]+\Z")
_REGION = re.compile(r"[A-Za-z0-9\-]+\Z")


def _bq_family(native: str) -> str:
    head = native.strip().upper()
    if head.startswith("ARRAY"):
        return "array"
    if head.startswith("STRUCT"):
        return "struct"
    if head.startswith(("NUMERIC", "BIGNUMERIC", "DECIMAL", "BIGDECIMAL")):
        return "decimal"
    if head.startswith(("INT64", "INT", "SMALLINT", "BIGINT", "TINYINT", "BYTEINT")):
        return "integer"
    if head.startswith("FLOAT"):
        return "float"
    if head.startswith("BOOL"):
        return "boolean"
    if head.startswith("DATETIME") or head.startswith("TIMESTAMP"):
        return "timestamp"
    if head.startswith("JSON"):
        return "json"
    if head.startswith(("STRING", "GEOGRAPHY", "INTERVAL", "RANGE")):
        return "string" if head.startswith("STRING") else "other"
    return family_of(native)


def _description(option_value: Any) -> str | None:
    """``TABLE_OPTIONS.option_value`` is a SQL literal: ``"text"`` with escapes."""
    if not isinstance(option_value, str):
        return None
    text = option_value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        try:
            decoded = json.loads(text) if text[0] == '"' else text[1:-1]
        except ValueError:
            decoded = text[1:-1]
        return str(decoded)
    return text


class BigQueryReader:
    dialect = "bigquery"

    def __init__(
        self,
        executor: MetadataExecutor,
        *,
        project: str | None = None,
        region: str = "us",
        page_size: int = 1000,
        timeout_s: float | None = None,
    ) -> None:
        if project is not None and not _PROJECT.match(project):
            raise CatalogError("SG104", f"unsupported characters in project {project!r}")
        if not _REGION.match(region):
            raise CatalogError("SG104", f"unsupported characters in region {region!r}")
        self._ex = executor
        self.project = project
        self.region = region
        self.page_size = page_size
        self.timeout_s = timeout_s
        self.bytes_billed = 0
        self.bytes_billed_is_lower_bound = False
        self._lock = threading.Lock()

    # -- plumbing ---------------------------------------------------------------------------
    def _region_view(self, view: str) -> str:
        prefix = f"`{self.project}`." if self.project else ""
        return f"{prefix}`region-{self.region}`.INFORMATION_SCHEMA.{view}"

    def _dataset_view(self, project: str | None, dataset: str, view: str) -> str:
        if not _DATASET.match(dataset):
            raise CatalogError("SG104", f"unsupported characters in dataset {dataset!r}")
        proj = project if project is not None else self.project
        if proj is not None and not _PROJECT.match(proj):
            raise CatalogError("SG104", f"unsupported characters in project {proj!r}")
        prefix = f"`{proj}`." if proj else ""
        return f"{prefix}`{dataset}`.INFORMATION_SCHEMA.{view}"

    def _run(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        rows = lower_keys(self._ex.run(sql, params, timeout_s=self.timeout_s))
        billed = getattr(self._ex, "last_bytes_billed", None)
        with self._lock:
            if isinstance(billed, int) and not isinstance(billed, bool):
                self.bytes_billed += billed
            else:
                self.bytes_billed += MIN_BILLED_BYTES
                self.bytes_billed_is_lower_bound = True
        return rows

    # -- protocol ---------------------------------------------------------------------------
    def list_catalogs(self) -> Iterator[str]:
        return iter((self.project,)) if self.project else iter(())

    def list_schemas(self, catalog: str | None, *, page_token: str | None) -> Page[Schema]:
        if page_token is not None:  # one page holds every schema: a later page is empty
            return Page([], None)
        project = catalog or self.project
        where = "WHERE catalog_name = ? " if project else ""
        rows = self._run(
            f"SELECT catalog_name, schema_name FROM {self._region_view('SCHEMATA')} "
            f"{where}ORDER BY schema_name",
            (project,) if project else (),
        )
        return Page([Schema(r["catalog_name"] or project, r["schema_name"]) for r in rows])

    def list_tables(
        self, schema: Schema, *, page_token: str | None, changed_since: datetime | None
    ) -> Page[Table]:
        t = self._dataset_view(schema.catalog, schema.name, "TABLES")
        o = self._dataset_view(schema.catalog, schema.name, "TABLE_OPTIONS")
        where: list[str] = []
        params: list[Any] = ["description"]
        if page_token is not None:
            where.append("t.table_name > ?")
            params.append(page_token)
        if changed_since is not None:
            # Views and tables without a TABLE_STORAGE row have no modified time: keep them
            # when they were created since, or when storage knows nothing about them.
            where.append(
                "(s.table_name IS NULL OR s.storage_last_modified_time >= ? "
                "OR t.creation_time >= ?)"
            )
            params += [changed_since, changed_since]
        params.append(self.page_size + 1)
        clause = f"WHERE {' AND '.join(where)} " if where else ""
        rows = self._run(
            "SELECT t.table_name AS table_name, t.table_type AS table_type, "
            "t.creation_time AS creation_time, d.option_value AS description, "
            "s.total_rows AS total_rows, s.total_logical_bytes AS total_logical_bytes, "
            "s.storage_last_modified_time AS last_modified "
            f"FROM {t} t "
            f"LEFT JOIN {o} d ON d.table_name = t.table_name AND d.option_name = ? "
            f"LEFT JOIN {self._region_view('TABLE_STORAGE')} s "
            "ON s.table_schema = t.table_schema AND s.table_name = t.table_name "
            "AND s.project_id = t.table_catalog AND NOT s.deleted "
            f"{clause}ORDER BY t.table_name LIMIT ?",
            params,
        )
        more = len(rows) > self.page_size
        rows = rows[: self.page_size]
        items = [
            Table(
                ObjectRef(schema.catalog, schema.name, r["table_name"]),
                _KINDS.get(str(r["table_type"]).upper(), "other"),
                _description(r["description"]),
                opt_int(r["total_rows"]),
                opt_int(r["total_logical_bytes"]),
                created_at=to_utc(r["creation_time"]),
                last_altered=to_utc(r["last_modified"]),
            )
            for r in rows
        ]
        return Page(items, rows[-1]["table_name"] if more else None)

    def columns(self, tables: Sequence[ObjectRef]) -> Mapping[ObjectRef, list[Column]]:
        out: dict[ObjectRef, list[Column]] = {t.table_ref(): [] for t in tables}
        for (catalog, schema), names in group_by_scope(tables).items():
            if schema is None:
                continue
            c = self._dataset_view(catalog, schema, "COLUMNS")
            f = self._dataset_view(catalog, schema, "COLUMN_FIELD_PATHS")
            marks = ", ".join("?" for _ in names)
            rows = self._run(
                "SELECT c.table_name AS table_name, c.column_name AS column_name, "
                "c.ordinal_position AS ordinal_position, c.data_type AS data_type, "
                "c.is_nullable AS is_nullable, c.column_default AS column_default, "
                "f.description AS description "
                f"FROM {c} c LEFT JOIN {f} f ON f.table_name = c.table_name "
                "AND f.column_name = c.column_name AND f.field_path = c.column_name "
                f"WHERE c.table_name IN ({marks}) "
                "ORDER BY c.table_name, c.ordinal_position",
                names,
            )
            for r in rows:
                tref = ObjectRef(catalog, schema, r["table_name"])
                default = r["column_default"]
                out.setdefault(tref, []).append(
                    Column(
                        ObjectRef(catalog, schema, r["table_name"], r["column_name"]),
                        int(r["ordinal_position"]),
                        r["data_type"],
                        _bq_family(r["data_type"]),
                        yes_no(r["is_nullable"]),
                        r["description"],
                        None if default is None or str(default).upper() == "NULL" else default,
                    )
                )
        return out

    def constraints(self, tables: Sequence[ObjectRef]) -> Mapping[ObjectRef, list[Constraint]]:
        out: dict[ObjectRef, list[Constraint]] = {t.table_ref(): [] for t in tables}
        for (catalog, schema), names in group_by_scope(tables).items():
            if schema is None:
                continue
            tc = self._dataset_view(catalog, schema, "TABLE_CONSTRAINTS")
            kcu = self._dataset_view(catalog, schema, "KEY_COLUMN_USAGE")
            ccu = self._dataset_view(catalog, schema, "CONSTRAINT_COLUMN_USAGE")
            marks = ", ".join("?" for _ in names)
            # An FK's referenced columns are matched to its own by position_in_unique_constraint
            # against the referenced PK's ordinal_position, so composite keys keep their order.
            rows = self._run(
                "SELECT k.table_name AS table_name, k.constraint_name AS constraint_name, "
                "tc.constraint_type AS constraint_type, tc.enforced AS enforced, "
                "k.column_name AS column_name, k.ordinal_position AS ordinal_position, "
                "k.position_in_unique_constraint AS unique_position, "
                "u.table_catalog AS ref_catalog, u.table_schema AS ref_schema, "
                "u.table_name AS ref_table, u.column_name AS ref_column, "
                "pk.ordinal_position AS ref_position "
                f"FROM {kcu} k JOIN {tc} tc ON tc.constraint_name = k.constraint_name "
                "AND tc.table_name = k.table_name "
                f"LEFT JOIN {ccu} u ON u.constraint_name = k.constraint_name "
                "AND tc.constraint_type = ? "
                f"LEFT JOIN {kcu} pk ON pk.table_name = u.table_name "
                "AND pk.column_name = u.column_name AND pk.constraint_name IN "
                f"(SELECT constraint_name FROM {tc} WHERE constraint_type = ?) "
                f"WHERE k.table_name IN ({marks}) "
                "ORDER BY k.table_name, k.constraint_name, k.ordinal_position",
                ("FOREIGN KEY", "PRIMARY KEY", *names),
            )
            for (table, _name), group in _by_constraint(rows).items():
                con = _bq_constraint(catalog, schema, table, group)
                if con is not None:
                    out.setdefault(ObjectRef(catalog, schema, table), []).append(con)
        return out

    def capabilities(self) -> ReaderCapabilities:
        return ReaderCapabilities(
            comments=True,
            stats=True,
            last_altered=True,
            constraints=True,
            incremental=True,
            cost_estimate=True,
        )


def _by_constraint(rows: Sequence[Row]) -> dict[tuple[str, str], list[Row]]:
    out: dict[tuple[str, str], list[Row]] = {}
    for r in rows:
        out.setdefault((r["table_name"], r["constraint_name"]), []).append(r)
    return out


def _bq_constraint(
    catalog: str | None, schema: str, table: str, group: Sequence[Row]
) -> Constraint | None:
    kind = _CONSTRAINTS.get(str(group[0]["constraint_type"]).upper())
    if kind is None:
        return None
    own = sorted({(int(r["ordinal_position"]), r["column_name"]) for r in group})
    columns = tuple(c for _, c in own)
    enforced = yes_no(group[0]["enforced"])
    tref = ObjectRef(catalog, schema, table)
    if kind != "foreign_key":
        return Constraint(kind, tref, columns, None, None, enforced)
    matched: dict[int, tuple[str, str, str, str, str]] = {}
    for r in group:
        if (
            r["ref_position"] is not None
            and r["unique_position"] is not None
            and int(r["ref_position"]) == int(r["unique_position"])
        ):
            matched[int(r["ordinal_position"])] = (
                r["column_name"],
                r["ref_catalog"],
                r["ref_schema"],
                r["ref_table"],
                r["ref_column"],
            )
    targets = {v[1:4] for v in matched.values()}
    if len(matched) != len(columns) or len(targets) != 1:
        return None  # referenced columns not resolvable in this dataset: unknown, not guessed
    rc, rs, rt = next(iter(targets))
    order = sorted(matched)
    return Constraint(
        kind,
        tref,
        tuple(matched[o][0] for o in order),
        ObjectRef(rc, rs, rt),
        tuple(matched[o][4] for o in order),
        enforced,
    )


register_reader("bigquery", BigQueryReader)
