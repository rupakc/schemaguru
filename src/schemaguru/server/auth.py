"""Request authentication for server mode (spec 17): a pluggable ``Authenticator`` and the v1
bearer-token implementation.

An authenticator maps request headers to a *role* (``"reader"`` or ``"admin"``) or ``None``.
Tokens are compared in constant time and never logged or echoed.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from collections.abc import Mapping
from typing import Protocol

from schemaguru.errors import ConfigError

ROLES = ("reader", "admin")
TOKENS_ENV = "SCHEMAGURU_SERVER_TOKENS"


class Authenticator(Protocol):
    def authenticate(self, headers: Mapping[str, str]) -> str | None:
        """The caller's role, or ``None`` when the request is not authenticated."""
        ...


def _digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


class BearerTokens:
    """``Authorization: Bearer <token>`` against a ``{token: role}`` map.

    Every configured token is compared (``hmac.compare_digest`` over SHA-256 digests, so length
    differences do not leak either), whether or not an earlier one matched.
    """

    def __init__(self, tokens: Mapping[str, str]) -> None:
        if not tokens:
            raise ConfigError("SG811: server mode needs at least one bearer token")
        entries: list[tuple[bytes, str]] = []
        for token, role in tokens.items():
            if role not in ROLES:
                raise ConfigError(f"SG811: unknown server role {role!r}; use one of {ROLES}")
            if len(token) < 16:
                raise ConfigError("SG811: a bearer token must be at least 16 characters")
            entries.append((_digest(token), role))
        self._entries = tuple(entries)

    def __repr__(self) -> str:  # never show tokens
        return f"BearerTokens(<{len(self._entries)} token(s)>)"

    def authenticate(self, headers: Mapping[str, str]) -> str | None:
        raw = headers.get("authorization") or headers.get("Authorization") or ""
        scheme, _, token = raw.strip().partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return None
        given = _digest(token.strip())
        found: str | None = None
        for digest, role in self._entries:
            if hmac.compare_digest(digest, given) and found is None:
                found = role
        return found

    @classmethod
    def from_env(cls, value: str | None = None) -> BearerTokens:
        """Parse ``token:role[,token:role...]`` (``SCHEMAGURU_SERVER_TOKENS`` by default).

        A token without ``:role`` is a ``reader``.
        """
        raw = os.environ.get(TOKENS_ENV, "") if value is None else value
        tokens: dict[str, str] = {}
        for item in raw.split(","):
            item = item.strip()
            if not item:
                continue
            token, sep, role = item.rpartition(":")
            if not sep:
                token, role = item, "reader"
            tokens[token.strip()] = role.strip()
        if not tokens:
            raise ConfigError(
                f"SG811: no bearer tokens configured; set {TOKENS_ENV}=<token>:admin,<token>:reader"
                " or pass tokens= / authenticator="
            )
        return cls(tokens)
