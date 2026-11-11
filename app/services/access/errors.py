"""Errors and small time helpers shared by the access services."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


class AccessError(Exception):
    """A refused access operation; ``code`` is stable (UI messages and tests key on it), never the password."""

    def __init__(self, code: str, message: str, status_code: int = 400, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware(value: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; treat them as UTC."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
