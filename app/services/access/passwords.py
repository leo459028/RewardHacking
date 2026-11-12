"""Password hashing and password rules for Dashboard accounts (v1.6.1 plan D12, D13).

Hashes use the standard library's ``hashlib.scrypt`` (no extra dependency) in a self-describing format,
``scrypt$<n>$<r>$<p>$<salt_b64>$<hash_b64>``, so the cost can be raised later and old hashes still verify; a
successful sign-in rehashes when the stored cost is below the configured one. Comparisons use
``hmac.compare_digest``. Hashing is CPU-bound, so request handlers use the ``*_async`` variants (a worker thread).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
from dataclasses import dataclass
from functools import lru_cache

from app.core.config import Settings

SCHEME = "scrypt"
SALT_BYTES = 16
KEY_BYTES = 32
MAX_PASSWORD_CHARS = 1024  # hashing very long inputs is a cheap denial of service; reject them instead

# D13: a small built-in list of passwords that are too common, compared case-insensitively. Short entries such as
# ``admin`` are already refused by the length rule; they stay here so the list also covers a lowered minimum.
COMMON_PASSWORDS = frozenset(
    {
        "admin",
        "password",
        "changeme",
        "administrator",
        "adminadmin12",
        "admin1234567",
        "password1234",
        "password123!",
        "passwordpassword",
        "changeme1234",
        "letmein12345",
        "welcome12345",
        "iloveyou1234",
        "qwerty123456",
        "qwertyuiop12",
        "1q2w3e4r5t6y",
        "abc123456789",
        "123456789012",
        "1234567890ab",
        "000000000000",
        "111111111111",
        "aaaaaaaaaaaa",
        "rewardhacking",
        "rewardhacking1",
        "guardrail1234",
        "dashboard1234",
    }
)


@dataclass(frozen=True)
class ScryptParams:
    n: int
    r: int
    p: int

    @property
    def maxmem(self) -> int:
        # scrypt needs about 128 * r * n bytes (plus 128 * r * p); OpenSSL's default limit (32 MiB) is too small for
        # n=2^15, r=8, so allow twice the requirement plus slack.
        return 2 * 128 * self.r * (self.n + self.p) + 1024 * 1024


def params_from_settings(settings: Settings) -> ScryptParams:
    return ScryptParams(
        n=int(settings.dashboard_password_scrypt_n),
        r=int(settings.dashboard_password_scrypt_r),
        p=int(settings.dashboard_password_scrypt_p),
    )


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _derive(password: str, salt: bytes, params: ScryptParams) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=params.n, r=params.r, p=params.p, maxmem=params.maxmem, dklen=KEY_BYTES
    )


def hash_password(password: str, params: ScryptParams) -> str:
    salt = os.urandom(SALT_BYTES)
    digest = _derive(password, salt, params)
    return f"{SCHEME}${params.n}${params.r}${params.p}${_b64(salt)}${_b64(digest)}"


def parse_hash(stored: str) -> tuple[ScryptParams, bytes, bytes] | None:
    """``(params, salt, digest)`` of a stored hash, or ``None`` when it is not a well-formed scrypt hash."""
    parts = (stored or "").split("$")
    if len(parts) != 6 or parts[0] != SCHEME:
        return None
    try:
        params = ScryptParams(n=int(parts[1]), r=int(parts[2]), p=int(parts[3]))
        salt = base64.b64decode(parts[4], validate=True)
        digest = base64.b64decode(parts[5], validate=True)
    except (ValueError, TypeError):
        return None
    if params.n < 2 or params.n & (params.n - 1) or params.r < 1 or params.p < 1 or not salt or not digest:
        return None
    return params, salt, digest


def verify_password(password: str, stored: str) -> bool:
    """Constant-time comparison; a malformed stored hash or an over-long password never verifies."""
    parsed = parse_hash(stored)
    if parsed is None or len(password) > MAX_PASSWORD_CHARS:
        return False
    params, salt, digest = parsed
    try:
        candidate = _derive(password, salt, params)
    except (ValueError, MemoryError):
        return False
    return hmac.compare_digest(candidate, digest)


def needs_rehash(stored: str, params: ScryptParams) -> bool:
    """D12: rehash after a successful sign-in when the stored cost is lower than (or different from) the setting."""
    parsed = parse_hash(stored)
    return parsed is None or parsed[0] != params


@lru_cache(maxsize=8)
def _dummy_hash(params: ScryptParams) -> str:
    return hash_password("rh-dummy-" + _b64(os.urandom(12)), params)


def dummy_verify(password: str, params: ScryptParams) -> bool:
    """D17: spend the same work as a real check when the account does not exist (always ``False``)."""
    verify_password(password, _dummy_hash(params))
    return False


async def hash_password_async(password: str, params: ScryptParams) -> str:
    return await asyncio.to_thread(hash_password, password, params)


async def verify_password_async(password: str, stored: str) -> bool:
    return await asyncio.to_thread(verify_password, password, stored)


async def dummy_verify_async(password: str, params: ScryptParams) -> bool:
    return await asyncio.to_thread(dummy_verify, password, params)


def password_problems(password: str, *, username: str, min_length: int) -> list[str]:
    """D13 rule check; an empty list means the password is acceptable. Never echoes the password.

    Codes: ``password_too_short``, ``password_too_long``, ``password_equals_username``, ``password_too_common``.
    Character-class composition is deliberately not required.
    """
    problems: list[str] = []
    if len(password) < min_length:
        problems.append("password_too_short")
    if len(password) > MAX_PASSWORD_CHARS:
        problems.append("password_too_long")
    lowered = password.strip().lower()
    if username and lowered == username.strip().lower():
        problems.append("password_equals_username")
    if lowered in COMMON_PASSWORDS or len(set(lowered)) <= 2:
        problems.append("password_too_common")
    return problems
