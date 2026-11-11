"""Shared helpers for the ``/v1`` API routers (v1.6.0: extracted from ``app/api/routes.py``)."""

from __future__ import annotations

from fastapi import HTTPException, Request

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


class RunPurgedError(Exception):
    """v1.6.0 D17: the run was removed by a retention purge; rendered as 410 ``run_purged`` with its tombstone."""

    def __init__(self, tombstone: dict) -> None:
        super().__init__("run_purged")
        self.tombstone = tombstone


async def raise_if_purged(session, run_id: str, auth_context: AuthContext) -> None:
    """Raise ``RunPurgedError`` when ``run_id`` has a tombstone (tenant-scoped like a live run)."""
    from app.services import retention

    tombstone = await retention.tombstone_for(session, run_id)
    if tombstone is None:
        return
    _query_tenant(tombstone.tenant_id, auth_context)
    raise RunPurgedError(retention.tombstone_summary(tombstone))


async def run_missing(session, run_id: str, auth_context: AuthContext) -> Exception:
    """Error for a run that is not in ``agent_runs``: 410 when it was purged, else the historical 404."""
    await raise_if_purged(session, run_id, auth_context)
    return HTTPException(status_code=404, detail="Run not found.")
