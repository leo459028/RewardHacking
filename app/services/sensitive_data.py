"""Sensitive key names and value patterns shared by the Safe MCP Gateway and evidence redaction.

v1.6.0: extracted from ``app.services.mcp_gateway`` into a dependency-free leaf module so evaluation code can use
them without importing the gateway (and, transitively, the FinOps lease code). See the import-linter contracts.
"""

from __future__ import annotations

import re

SENSITIVE_KEY_NAMES = frozenset(
    {
        "api_key",
        "apikey",
        "secret",
        "password",
        "passwd",
        "token",
        "access_token",
        "refresh_token",
        "authorization",
        "private_key",
        "client_secret",
        "credential",
        "credentials",
    }
)
SENSITIVE_KEY_SUFFIXES = ("_token", "_secret", "_password", "_passwd", "_api_key", "_apikey", "_credential")

SENSITIVE_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"rk_[A-Za-z0-9_-]{6,}"),
    re.compile(r"rg_[A-Za-z0-9_-]{6,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._\-~+/]+=*"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
)
