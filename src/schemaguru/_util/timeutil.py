"""Timestamps are tz-aware UTC; naive ones are rejected (spec 25.19)."""

from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def require_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"naive datetime rejected (timestamps must be tz-aware): {dt!r}")
    return dt


def to_iso(dt: datetime) -> str:
    """Fixed-width UTC text (``2026-11-24T00:00:00.000000Z``) that sorts chronologically."""
    return require_aware(dt).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
