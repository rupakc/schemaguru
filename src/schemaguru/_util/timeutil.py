"""Timestamps are tz-aware UTC; naive ones are rejected (spec 25.19)."""

from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def require_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"naive datetime rejected (timestamps must be tz-aware): {dt!r}")
    return dt
