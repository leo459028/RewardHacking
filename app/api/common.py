"""Shared helpers for the ``/v1`` API routers (v1.6.0: extracted from ``app/api/routes.py``)."""

from __future__ import annotations

from fastapi import Request

from app.services.auth import AuthContext, auth_error


def _query_tenant(tenant_id: str | None, auth_context: AuthContext) -> str | None:
    if auth_context.enabled:
        auth_tenant = auth_context.tenant_id or "default"
        if tenant_id and tenant_id != auth_tenant:
            raise auth_error(403, "tenant_mismatch", "Query tenant_id does not match API key tenant.")
        return auth_tenant
    return tenant_id


def _actor(auth_context: AuthContext) -> str:
    if auth_context.key_id:
        return f"key:{auth_context.key_id}"
    if auth_context.enabled:
        return f"tenant:{auth_context.tenant_id or 'default'}"
    return "dashboard"


def _trace_id(request: Request) -> str:
    return str(getattr(request.state, "trace_id", ""))
