"""Exception base (layer 0). Library code raises only for programmer errors and store errors."""

from __future__ import annotations


class SchemaGuruError(Exception):
    """Base class of every exception SchemaGuru raises on purpose."""


class StoreVersionError(SchemaGuruError):
    """A store was written by a newer (or unknown) schema version than this library supports."""


class AmbiguousNameError(SchemaGuruError):
    """A bare name matches several objects; ``candidates`` lists them (never a guess)."""

    def __init__(self, message: str, candidates: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.candidates = candidates


class ConfigError(SchemaGuruError):
    """Invalid configuration, e.g. an unknown key or a value outside its allowed set."""
