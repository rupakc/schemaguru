"""Oracle reader over the ``ALL_*`` dictionary views (spec 6.2).

Schema = owner; refs carry no catalog. Names are upper-case unless created quoted and are
stored exactly as returned. Needs Oracle 12.1+ (``FETCH FIRST``, ``ALL_USERS.ORACLE_MAINTAINED``).
``ALL_*`` views show only objects the user may access. ``NUM_ROWS`` is the optimizer-statistics
estimate (unknown until stats are gathered). Oracle ``DATE`` carries a time part, so it maps to
the ``timestamp`` family. Dictionary timestamps are zone-less, so ``created_at``/``last_altered``
stay unknown. Constraint ``ENABLED`` status is reported as ``enforced``.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime
from typing import Any

from schemaguru.catalog.executor import MetadataExecutor
from schemaguru.catalog.protocol import Page, ReaderCapabilities
from schemaguru.catalog.readers._common import Row, definition_text, lower_keys, opt_int, pairs
from schemaguru.catalog.readers._types import family_of
from schemaguru.catalog.registry import register_reader
from schemaguru.diagnostics import Diagnostic, Severity
from schemaguru.model.refs import ObjectRef
from schemaguru.model.structure import Column, Constraint, ConstraintKind, Schema, Table, TableKind

_KINDS: Mapping[str, TableKind] = {
    "TABLE": "table",
    "VIEW": "view",
    "MATERIALIZED VIEW": "materialized_view",
}
_CONTYPE: Mapping[str, ConstraintKind] = {
    "P": "primary_key",
    "U": "unique",
    "R": "foreign_key",
}
_CHAR_TYPES = ("VARCHAR2", "NVARCHAR2", "CHAR", "NCHAR")


def _ora_family(data_type: str, scale: Any) -> str:
    head = data_type.strip().upper()
    if head == "NUMBER" or head == "INTEGER":
        s = opt_int(scale)
        return "integer" if s == 0 or head == "INTEGER" else "decimal"
    if head in ("FLOAT", "BINARY_FLOAT", "BINARY_DOUBLE"):
        return "float"
    if head in ("DATE",) or head.startswith("TIMESTAMP"):
        return "timestamp"
    if head.startswith(("VARCHAR", "NVARCHAR", "CHAR", "NCHAR", "CLOB", "NCLOB", "LONG", "ROWID")):
        return "bytes" if head == "LONG RAW" else "string"
    if head in ("RAW", "BLOB", "BFILE", "LONG RAW"):
        return "bytes"
    if head == "JSON":
        return "json"
    if head.startswith("INTERVAL"):
        return "other"
    return family_of(data_type) if head not in ("XMLTYPE",) else "other"


def _native(r: Row) -> str:
    dt = str(r["data_type"])
    if dt in _CHAR_TYPES and r.get("char_length"):
        return f"{dt}({int(r['char_length'])})"
    if dt == "RAW" and r.get("data_length") is not None:
        return f"RAW({int(r['data_length'])})"
    if dt == "NUMBER" and r.get("data_precision") is not None:
        return f"NUMBER({int(r['data_precision'])},{int(r['data_scale'] or 0)})"
    return dt


class OracleReader:
    dialect = "oracle"

    def __init__(
        self,
        executor: MetadataExecutor,
        *,
        page_size: int = 1000,
        timeout_s: float | None = None,
    ) -> None:
        self._ex = executor
        self.diagnostics: list[Diagnostic] = []
        self.page_size = page_size
        self.timeout_s = timeout_s

    def _run(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        return lower_keys(self._ex.run(sql, params, timeout_s=self.timeout_s))

    def list_catalogs(self) -> Iterator[str]:
        return iter(())

    def list_schemas(self, catalog: str | None, *, page_token: str | None) -> Page[Schema]:
        if page_token is not None:  # one page holds every schema: a later page is empty
            return Page([], None)
        rows = self._run(
            "SELECT u.username AS name, "
            "(SELECT COUNT(*) FROM all_tables t WHERE t.owner = u.username) "
            "+ (SELECT COUNT(*) FROM all_views v WHERE v.owner = u.username) AS table_count "
            "FROM all_users u WHERE u.oracle_maintained = ? ORDER BY u.username",
            ("N",),
        )
        return Page([Schema(None, r["name"], None, opt_int(r["table_count"])) for r in rows])

    def list_tables(
        self, schema: Schema, *, page_token: str | None, changed_since: datetime | None
    ) -> Page[Table]:
        keyset = "" if page_token is None else "WHERE x.table_name > ? "
        params: list[Any] = [schema.name] * 5
        if page_token is not None:
            params.append(page_token)
        params.append(self.page_size + 1)
        rows = self._run(
            "SELECT x.table_name AS table_name, x.kind AS kind, x.num_rows AS num_rows, "
            "CASE WHEN x.kind = 'MATERIALIZED VIEW' THEN x.mv_comment ELSE c.comments END "
            "AS comments, p.partitioning_type AS partitioning_type FROM ("
            " SELECT t.table_name AS table_name, 'TABLE' AS kind, t.num_rows AS num_rows,"
            "  CAST(NULL AS VARCHAR2(4000)) AS mv_comment"
            "  FROM all_tables t WHERE t.owner = ? AND t.nested = 'NO' AND t.dropped = 'NO'"
            "  AND t.iot_name IS NULL AND NOT EXISTS (SELECT 1 FROM all_mviews m"
            "   WHERE m.owner = t.owner AND m.mview_name = t.table_name)"
            " UNION ALL"
            " SELECT v.view_name, 'VIEW', NULL, NULL FROM all_views v WHERE v.owner = ?"
            " UNION ALL"
            " SELECT m.mview_name, 'MATERIALIZED VIEW', NULL, mc.comments FROM all_mviews m"
            "  LEFT JOIN all_mview_comments mc ON mc.owner = m.owner"
            "  AND mc.mview_name = m.mview_name WHERE m.owner = ?"
            ") x "
            "LEFT JOIN all_tab_comments c ON c.owner = ? AND c.table_name = x.table_name "
            "LEFT JOIN all_part_tables p ON p.owner = ? AND p.table_name = x.table_name "
            f"{keyset}ORDER BY x.table_name FETCH FIRST ? ROWS ONLY",
            params,
        )
        more = len(rows) > self.page_size
        rows = rows[: self.page_size]
        items = [
            Table(
                ObjectRef(None, schema.name, r["table_name"]),
                _KINDS.get(str(r["kind"]), "other"),
                r["comments"] or None,
                opt_int(r["num_rows"]),
                partitioning=r["partitioning_type"],
            )
            for r in rows
        ]
        return Page(items, rows[-1]["table_name"] if more else None)

    def columns(self, tables: Sequence[ObjectRef]) -> Mapping[ObjectRef, list[Column]]:
        out: dict[ObjectRef, list[Column]] = {t.table_ref(): [] for t in tables}
        if not tables:
            return out
        clause, params = pairs(tables, "c.owner", "c.table_name")
        rows = self._run(
            "SELECT c.owner AS owner, c.table_name AS table_name, c.column_name AS column_name, "
            "c.column_id AS column_id, c.data_type AS data_type, c.data_length AS data_length, "
            "c.char_length AS char_length, c.data_precision AS data_precision, "
            "c.data_scale AS data_scale, c.nullable AS nullable, c.data_default AS data_default, "
            "m.comments AS comments "
            "FROM all_tab_columns c LEFT JOIN all_col_comments m ON m.owner = c.owner "
            "AND m.table_name = c.table_name AND m.column_name = c.column_name "
            f"WHERE ({clause}) "
            "ORDER BY c.owner, c.table_name, c.column_id",
            params,
        )
        for r in rows:
            default = r["data_default"]
            out.setdefault(ObjectRef(None, r["owner"], r["table_name"]), []).append(
                Column(
                    ObjectRef(None, r["owner"], r["table_name"], r["column_name"]),
                    int(r["column_id"]),
                    _native(r),
                    _ora_family(str(r["data_type"]), r["data_scale"]),
                    {"Y": True, "N": False}.get(str(r["nullable"]).upper()),
                    r["comments"] or None,
                    str(default).strip() if default is not None else None,
                )
            )
        return out

    def constraints(self, tables: Sequence[ObjectRef]) -> Mapping[ObjectRef, list[Constraint]]:
        out: dict[ObjectRef, list[Constraint]] = {t.table_ref(): [] for t in tables}
        if not tables:
            return out
        clause, params = pairs(tables, "k.owner", "k.table_name")
        rows = self._run(
            "SELECT k.owner AS owner, k.table_name AS table_name, "
            "k.constraint_name AS constraint_name, k.constraint_type AS constraint_type, "
            "k.status AS status, cc.column_name AS column_name, cc.position AS position, "
            "rc.owner AS ref_owner, rc.table_name AS ref_table, rcc.column_name AS ref_column "
            "FROM all_constraints k "
            "JOIN all_cons_columns cc ON cc.owner = k.owner "
            "AND cc.constraint_name = k.constraint_name "
            "LEFT JOIN all_constraints rc ON rc.owner = k.r_owner "
            "AND rc.constraint_name = k.r_constraint_name "
            "LEFT JOIN all_cons_columns rcc ON rcc.owner = rc.owner "
            "AND rcc.constraint_name = rc.constraint_name AND rcc.position = cc.position "
            f"WHERE k.constraint_type IN (?, ?, ?) AND ({clause}) "
            "ORDER BY k.owner, k.table_name, k.constraint_name, cc.position",
            ("P", "U", "R", *params),
        )
        grouped: dict[tuple[str, str, str], list[Row]] = {}
        for r in rows:
            grouped.setdefault((r["owner"], r["table_name"], r["constraint_name"]), []).append(r)
        for (owner, table, _name), grp in grouped.items():
            kind = _CONTYPE[str(grp[0]["constraint_type"]).upper()]
            tref = ObjectRef(None, owner, table)
            ref_table = ref_cols = None
            if kind == "foreign_key":
                if any(g["ref_column"] is None for g in grp):
                    continue
                ref_table = ObjectRef(None, grp[0]["ref_owner"], grp[0]["ref_table"])
                ref_cols = tuple(g["ref_column"] for g in grp)
            enforced = {"ENABLED": True, "DISABLED": False}.get(str(grp[0]["status"]).upper())
            out.setdefault(tref, []).append(
                Constraint(
                    kind, tref, tuple(g["column_name"] for g in grp), ref_table, ref_cols, enforced
                )
            )
        return out

    def view_definitions(self, tables: Sequence[ObjectRef]) -> Mapping[ObjectRef, str]:
        """``ALL_VIEWS.TEXT`` and ``ALL_MVIEWS.QUERY``. Both are LONG columns: they cannot be
        joined or unioned, so each is read by its own single-table query and arrives as text
        through the driver's ordinary fetch. A query that fails leaves its definitions absent and
        notes ``SG102`` (the error's type only: never its text, which may echo the SQL)."""
        out: dict[ObjectRef, str] = {}
        if not tables:
            return out
        sources = (("all_views", "view_name", "text"), ("all_mviews", "mview_name", "query"))
        for rel, name_col, text_col in sources:
            clause, params = pairs(tables, "owner", name_col)
            try:
                rows = self._run(
                    f"SELECT owner, {name_col} AS name, {text_col} AS definition FROM {rel} "
                    f"WHERE ({clause}) ORDER BY owner, {name_col}",
                    params,
                )
            except Exception as e:
                self.diagnostics.append(
                    Diagnostic(
                        "SG102",
                        Severity.WARNING,
                        f"{rel} definitions not read ({type(e).__name__})",
                        hint="the views are still crawled; their definitions are unknown",
                    )
                )
                continue
            for r in rows:
                text = definition_text(r["definition"])
                if text is not None:
                    out[ObjectRef(None, r["owner"], r["name"])] = text
        return out

    def capabilities(self) -> ReaderCapabilities:
        return ReaderCapabilities(
            comments=True, stats=True, constraints=True, view_definitions=True
        )


register_reader("oracle", OracleReader)
