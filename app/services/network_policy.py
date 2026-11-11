"""Internal / external endpoint classification (v1.6.0: extracted from ``app.services.mcp_gateway``).

Dependency-free leaf module shared by the Safe MCP Gateway and the LLM judges.
"""

from __future__ import annotations

from urllib.parse import urlsplit


def is_external_endpoint(transport: str, endpoint: str, internal_suffixes: list[str]) -> bool:
    """Return True when the endpoint leaves the internal network.

    Uses parsed hostnames (never string prefixes), so ``http://localhost.evil.com``
    is external. Unparseable endpoints fail closed (external).
    """
    if transport == "stdio":
        return False
    raw = (endpoint or "").strip()
    if not raw:
        return True
    try:
        parts = urlsplit(raw)
    except ValueError:
        return True
    if parts.scheme == "local":
        return False
    try:
        host = (parts.hostname or "").lower().rstrip(".")
    except ValueError:
        return True
    if not host:
        return True
    if host in {"localhost", "127.0.0.1", "::1"}:
        return False
    for suffix in internal_suffixes:
        suffix = suffix.lower()
        if suffix and host.endswith(suffix) and len(host) > len(suffix):
            return False
    return True
