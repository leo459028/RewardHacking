"""Canonical JSON and SHA-256 digests shared by retention tombstones and the v1.6.1 audit chain.

Moved from ``app.services.retention`` in v1.6.1 (plan D29) without changing the output: keys sorted, compact
separators, UTF-8 kept, datetimes as UTC ISO-8601 (naive values are treated as UTC), ``Decimal`` in plain
notation and bytes replaced by their SHA-256. Changing any of this changes every stored digest.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def iso_utc(value: datetime | None) -> str | None:
    """UTC ISO-8601 for a datetime; SQLite returns naive datetimes, which are treated as UTC."""
    value = _aware(value)
    return value.astimezone(timezone.utc).isoformat() if value else None


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return iso_utc(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (bytes, bytearray)):
        return hashlib.sha256(bytes(value)).hexdigest()
    return str(value)


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_json_default)


def sha256_hex(payload: Any) -> str:
    """SHA-256 of a string as is, or of the canonical JSON of any other value."""
    raw = payload if isinstance(payload, str) else canonical_json(payload)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


__all__ = ["canonical_json", "iso_utc", "sha256_hex"]
