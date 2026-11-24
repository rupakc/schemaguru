"""Credential redaction (``schemaguru._util.redact``): URLs and ``key=value`` secrets."""

from __future__ import annotations

import pytest

from schemaguru._util.redact import redact_url, scrub_text

SECRETS = ("p4ssw0rd!", "hunter2", "Sg_Passw0rd!", "s3cr3t-value", "abc")


@pytest.mark.parametrize(
    "text",
    [
        "cannot connect to snowflake://u:p4ssw0rd!@acct/db",
        "login failed: password=hunter2",
        "login failed (password=hunter2)",
        "DRIVER={X};SERVER=h,1433;UID=sa;PWD=Sg_Passw0rd!;TrustServerCertificate=yes",
        "db_password: s3cr3t-value in the config",
        "x_secret=abc",
        "Password = 'hunter2'",
        'api_key="s3cr3t-value"',
        "token=abc&next=1",
    ],
)
def test_scrub_text_removes_every_credential(text: str) -> None:
    out = scrub_text(text)
    for secret in SECRETS:
        assert secret not in out, (text, out)
    assert out != text


def test_scrub_text_keeps_ordinary_text() -> None:
    """Negative control: words that merely look like keys, and a parse error's token, stay."""
    for text in (
        "unexpected token: FROM at line 3",
        "the password column is masked",
        "column customer_key = 42 is not unique",
        "no such table shop.orders",
    ):
        assert scrub_text(text) == text


def test_duckdb_file_path_is_shown() -> None:
    """A local database path is not a secret: it stays readable in errors."""
    assert redact_url("duckdb:///data/warehouse.duckdb") == "duckdb:///data/warehouse.duckdb"
    assert redact_url("duckdb:///warehouse.duckdb") == "duckdb:///warehouse.duckdb"
    assert redact_url("sqlite:///x/y.db") == "sqlite:///x/y.db"
    msg = scrub_text("no DuckDB database file at duckdb:///data/w.duckdb")
    assert "/data/w.duckdb" in msg
    # but a query string (tokens) or userinfo is still never shown
    assert "tok" not in redact_url("duckdb:///w.duckdb?motherduck_token=tok")
    assert "pw" not in redact_url("duckdb://u:pw@host/w.duckdb")
    # and other schemes still drop the path (negative control)
    assert redact_url("postgresql://u:pw@db.example:5432/shop") == "postgresql://db.example:5432"
