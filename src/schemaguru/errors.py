"""Exception base (layer 0). Library code raises only for programmer errors and store errors."""

from __future__ import annotations

# What an agent or HTTP client is told about a shared store's trouble: fixed text, never the
# store's address, user or driver message (those go to the host's logs and exceptions)
STORE_UNAVAILABLE = "the knowledge store is unavailable; retry later"
STORE_MISCONFIGURED = "the knowledge store is misconfigured (its credentials were rejected)"


class SchemaGuruError(Exception):
    """Base class of every exception SchemaGuru raises on purpose."""


class StoreVersionError(SchemaGuruError):
    """A store was written by a newer (or unknown) schema version than this library supports.

    ``code`` is the diagnostic code (``SG702``), so a caller need not parse the message.
    """

    code = "SG702"


class AmbiguousNameError(SchemaGuruError):
    """A bare name matches several objects; ``candidates`` lists them (never a guess)."""

    def __init__(self, message: str, candidates: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.candidates = candidates


class ConfigError(SchemaGuruError):
    """Invalid configuration, e.g. an unknown key or a value outside its allowed set."""


class StoreUnavailableError(SchemaGuruError):
    """A shared store cannot be reached right now (connection refused, lost, pool exhausted)."""


class StoreConfigError(ConfigError):
    """A shared store rejected the configured credentials: the host's configuration, not an
    outage (``SG704``; retrying will not help)."""
