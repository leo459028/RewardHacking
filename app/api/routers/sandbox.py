"""Sandbox status and conformance endpoints (v1.6.0 D21-D24, v1.6.2 D14, D40).

The API never reaches Docker: it reads the gate from the database and queues work for ``sandbox-runner``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.common import _actor
from app.api.deps import get_state_store
from app.core.config import get_settings
from app.infra.postgres import get_session
from app.schemas.sandbox import SandboxStatusResponse
from app.services.auth import AuthContext, get_auth_context, require_role
from app.services.run_state import RunStateStore
from app.services.sandbox import gate, jobs
from app.services.sandbox.health import status_report
from app.services.sandbox.registry import DOCKER, configured_backend

router = APIRouter(prefix="/v1", tags=["sandbox"])


@router.get("/sandbox/status", response_model=SandboxStatusResponse)
async def get_sandbox_status(
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
    state_store: RunStateStore = Depends(get_state_store),
):
    require_role(auth_context, "viewer", "operator", "admin")
    settings = get_settings()
    status = await gate.evaluate(session, settings)
    return {**status_report(settings), "gate": status.to_dict(), "runners": await jobs.runner_heartbeats(state_store)}


@router.post("/sandbox/conformance", status_code=202)
async def request_sandbox_conformance(
    auth_context: AuthContext = Depends(get_auth_context), session: AsyncSession = Depends(get_session)
) -> JSONResponse:
    """Queue a full conformance run for ``sandbox-runner``; an active run is returned instead of queueing another."""
    require_role(auth_context, "admin")
    settings = get_settings()
    if configured_backend(settings) != DOCKER or not settings.sandbox_execution_enabled:
        raise HTTPException(
            status_code=409,
            detail={"code": "sandbox_execution_disabled", "message": "Set SANDBOX_BACKEND=docker and SANDBOX_EXECUTION_ENABLED=true first."},
        )
    job, created = await jobs.enqueue_conformance(session, requested_by=_actor(auth_context))
    await session.commit()
    return JSONResponse(status_code=202, content={"job": jobs.job_to_dict(job), "created": created})


@router.get("/sandbox/conformance")
async def list_sandbox_conformance(
    limit: int = Query(default=20, ge=1, le=200),
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    require_role(auth_context, "admin", "auditor")
    rows = await jobs.list_conformance(session, limit=limit)
    return {"runs": [jobs.conformance_to_dict(row) for row in rows], "count": len(rows)}
