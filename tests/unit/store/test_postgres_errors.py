"""Postgres store connection errors without a server (T21): psycopg's connect is replaced."""

from __future__ import annotations

from typing import Any

import pytest

psycopg = pytest.importorskip("psycopg")  # a dev dependency (mypy checks the store against it)


def _refusing(message: str) -> Any:
    def connect(*a: Any, **k: Any) -> Any:
        raise psycopg.OperationalError(message)

    return connect


def test_password_rejected_is_a_configuration_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from schemaguru.errors import ConfigError, StoreConfigError, StoreUnavailableError
    from schemaguru.store.postgres import PostgresStore

    monkeypatch.setattr(
        psycopg,
        "connect",
        _refusing(
            'connection failed: connection to server at "10.0.0.7", port 5432 failed: '
            'FATAL:  password authentication failed for user "sg"'
        ),
    )
    with pytest.raises(StoreConfigError) as info:
        PostgresStore("postgresql://sg:s3cret-pw@10.0.0.7:5432/kb")
    err = info.value
    assert isinstance(err, ConfigError) and not isinstance(err, StoreUnavailableError)
    assert str(err).startswith("SG704") and "s3cret" not in str(err)


def test_refused_connection_stays_an_outage(monkeypatch: pytest.MonkeyPatch) -> None:
    from schemaguru.errors import StoreUnavailableError
    from schemaguru.store.postgres import PostgresStore

    monkeypatch.setattr(psycopg, "connect", _refusing("connection refused"))
    with pytest.raises(StoreUnavailableError, match="SG703"):
        PostgresStore("postgresql://sg:s3cret-pw@10.0.0.7:5432/kb")


class _Diag:
    def __init__(self, sqlstate: str | None) -> None:
        self.sqlstate = sqlstate


def _with_state(message: str, state: str | None) -> Any:
    class Refused(psycopg.OperationalError):  # psycopg exposes the SQLSTATE on .diag
        @property
        def diag(self) -> Any:
            return _Diag(state)

    def connect(*a: Any, **k: Any) -> Any:
        raise Refused(message)

    return connect


@pytest.mark.parametrize(
    ("message", "state", "config"),
    [
        ("FATAL:  Passwort-Authentifizierung fehlgeschlagen", "28P01", True),  # any language
        ('FATAL:  role "nobody" does not exist', "28000", True),  # a missing role
        ("server closed the connection unexpectedly", "08006", False),  # an outage
        ('FATAL:  role "nobody" does not exist', None, True),  # no SQLSTATE: the text decides
    ],
)
def test_sqlstate_decides_before_the_text(
    monkeypatch: pytest.MonkeyPatch, message: str, state: str | None, config: bool
) -> None:
    from schemaguru.errors import StoreConfigError, StoreUnavailableError
    from schemaguru.store.postgres import PostgresStore

    monkeypatch.setattr(psycopg, "connect", _with_state(message, state))
    expected = StoreConfigError if config else StoreUnavailableError
    with pytest.raises(expected):
        PostgresStore("postgresql://sg:pw@10.0.0.7:5432/kb")
