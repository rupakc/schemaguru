"""Live warehouse reader checks, one per dialect, skipped unless explicitly enabled.

Set ``SCHEMAGURU_LIVE_<DIALECT>=1`` plus that dialect's connection variables (listed in
``docs/wiki/Warehouse-<Dialect>.md``) and install its driver. Each test points at ONE schema
(``..._SCHEMA`` / ``..._DATASET``) so a run reads a handful of objects, and asserts structure
only (no row data is ever read). BigQuery bills every query (>= 10 MB), Snowflake uses warehouse
credits: run deliberately.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from typing import Any

import pytest

from schemaguru.catalog import DBAPIExecutor, MetadataExecutor, reader_for
from schemaguru.catalog.protocol import CatalogReader
from schemaguru.model.structure import Schema

pytestmark = pytest.mark.live


def _need(dialect: str, *names: str) -> dict[str, str]:
    if os.environ.get(f"SCHEMAGURU_LIVE_{dialect.upper()}") != "1":
        pytest.skip(f"set SCHEMAGURU_LIVE_{dialect.upper()}=1 to run against a real account")
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        pytest.skip(f"missing credentials: {', '.join(missing)}")
    return {n: os.environ[n] for n in names}


def _walk(reader: CatalogReader, schema: Schema) -> None:
    """The structural assertions every live reader must meet."""
    tables = reader.list_tables(schema, page_token=None, changed_since=None).items
    assert tables, f"no tables visible in {schema.key()}"
    refs = [t.ref for t in tables[:20]]
    cols = reader.columns(refs)
    assert any(cols.values()), "no columns returned"
    for lst in cols.values():
        assert [c.ordinal for c in lst] == sorted(c.ordinal for c in lst)
        assert all(c.family for c in lst)
    cons = reader.constraints(refs)
    assert set(cons) >= set(refs)  # every requested table is answered, possibly with []


def _pick(reader: CatalogReader, catalog: str | None, name: str) -> Schema:
    for s in reader.list_schemas(catalog, page_token=None).items:
        if s.name == name:
            return s
    pytest.fail(f"schema {name!r} not listed")


class _BigQueryExecutor:
    """Positional-parameter adapter over google-cloud-bigquery that also reports bytes billed."""

    def __init__(self, client: Any) -> None:
        self._c = client
        self.last_bytes_billed: int | None = None

    def run(
        self, sql: str, params: Sequence[Any] = (), *, timeout_s: float | None = None
    ) -> list[dict[str, Any]]:
        from google.cloud import bigquery

        def typed(v: Any) -> Any:
            kind = (
                "INT64" if isinstance(v, int) else "TIMESTAMP" if hasattr(v, "tzinfo") else "STRING"
            )
            return bigquery.ScalarQueryParameter(None, kind, v)

        cfg = bigquery.QueryJobConfig(query_parameters=[typed(p) for p in params])
        job = self._c.query(sql, job_config=cfg)
        rows = [dict(r.items()) for r in job.result(timeout=timeout_s)]
        self.last_bytes_billed = job.total_bytes_billed
        return rows


def test_live_bigquery() -> None:
    env = _need(
        "bigquery", "SCHEMAGURU_BQ_PROJECT", "SCHEMAGURU_BQ_REGION", "SCHEMAGURU_BQ_DATASET"
    )
    bigquery = pytest.importorskip("google.cloud.bigquery")
    from schemaguru.catalog.readers.bigquery import BigQueryReader

    ex = _BigQueryExecutor(bigquery.Client(project=env["SCHEMAGURU_BQ_PROJECT"]))
    reader = BigQueryReader(
        ex,  # type: ignore[arg-type]
        project=env["SCHEMAGURU_BQ_PROJECT"],
        region=env["SCHEMAGURU_BQ_REGION"],
    )
    _walk(reader, _pick(reader, env["SCHEMAGURU_BQ_PROJECT"], env["SCHEMAGURU_BQ_DATASET"]))
    assert reader.bytes_billed > 0


def _dbapi(connect: Callable[[], Any], paramstyle: str) -> MetadataExecutor:
    return DBAPIExecutor(connect(), paramstyle)


def test_live_snowflake() -> None:
    env = _need(
        "snowflake",
        "SCHEMAGURU_SF_ACCOUNT",
        "SCHEMAGURU_SF_USER",
        "SCHEMAGURU_SF_PASSWORD",
        "SCHEMAGURU_SF_WAREHOUSE",
        "SCHEMAGURU_SF_DATABASE",
        "SCHEMAGURU_SF_SCHEMA",
    )
    sf = pytest.importorskip("snowflake.connector")
    sf.paramstyle = "qmark"
    ex = _dbapi(
        lambda: sf.connect(
            account=env["SCHEMAGURU_SF_ACCOUNT"],
            user=env["SCHEMAGURU_SF_USER"],
            password=env["SCHEMAGURU_SF_PASSWORD"],
            warehouse=env["SCHEMAGURU_SF_WAREHOUSE"],
        ),
        "qmark",
    )
    reader = reader_for("snowflake", ex)
    db = env["SCHEMAGURU_SF_DATABASE"]
    _walk(reader, _pick(reader, db, env["SCHEMAGURU_SF_SCHEMA"]))


def test_live_redshift() -> None:
    env = _need(
        "redshift",
        "SCHEMAGURU_RS_HOST",
        "SCHEMAGURU_RS_DATABASE",
        "SCHEMAGURU_RS_USER",
        "SCHEMAGURU_RS_PASSWORD",
        "SCHEMAGURU_RS_SCHEMA",
    )
    rs = pytest.importorskip("redshift_connector")
    ex = _dbapi(
        lambda: rs.connect(
            host=env["SCHEMAGURU_RS_HOST"],
            database=env["SCHEMAGURU_RS_DATABASE"],
            user=env["SCHEMAGURU_RS_USER"],
            password=env["SCHEMAGURU_RS_PASSWORD"],
        ),
        "format",
    )
    reader = reader_for("redshift", ex)
    _walk(reader, _pick(reader, env["SCHEMAGURU_RS_DATABASE"], env["SCHEMAGURU_RS_SCHEMA"]))


def test_live_databricks() -> None:
    env = _need(
        "databricks",
        "SCHEMAGURU_DBX_HOST",
        "SCHEMAGURU_DBX_HTTP_PATH",
        "SCHEMAGURU_DBX_TOKEN",
        "SCHEMAGURU_DBX_CATALOG",
        "SCHEMAGURU_DBX_SCHEMA",
    )
    dbsql = pytest.importorskip("databricks.sql")
    ex = _dbapi(
        lambda: dbsql.connect(
            server_hostname=env["SCHEMAGURU_DBX_HOST"],
            http_path=env["SCHEMAGURU_DBX_HTTP_PATH"],
            access_token=env["SCHEMAGURU_DBX_TOKEN"],
        ),
        "qmark",
    )
    reader = reader_for("databricks", ex)
    _walk(reader, _pick(reader, env["SCHEMAGURU_DBX_CATALOG"], env["SCHEMAGURU_DBX_SCHEMA"]))


def test_live_oracle() -> None:
    env = _need(
        "oracle",
        "SCHEMAGURU_ORA_DSN",
        "SCHEMAGURU_ORA_USER",
        "SCHEMAGURU_ORA_PASSWORD",
        "SCHEMAGURU_ORA_SCHEMA",
    )
    oracledb = pytest.importorskip("oracledb")
    ex = _dbapi(
        lambda: oracledb.connect(
            user=env["SCHEMAGURU_ORA_USER"],
            password=env["SCHEMAGURU_ORA_PASSWORD"],
            dsn=env["SCHEMAGURU_ORA_DSN"],
        ),
        "numeric",
    )
    reader = reader_for("oracle", ex)
    _walk(reader, _pick(reader, None, env["SCHEMAGURU_ORA_SCHEMA"]))
