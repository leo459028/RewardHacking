"""Dry-run replay of a run (moved from ``app/api/routes.py`` in v1.6.2 under the router split rule; paths are unchanged)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.common import _actor, _query_tenant, run_missing
from app.core.config import get_settings
from app.infra.postgres import get_session
from app.schemas.checkpoint import ReplayAttemptRecord, ReplayRunRequest
from app.services.audit_log import get_run
from app.services.auth import AuthContext, get_auth_context, require_role
from app.services.checkpoint import persist_replay_attempt, persist_sandbox_replay, replay_attempt_to_dict
from app.services.control_plane import get_execution_plan_for_run, list_execution_plan_steps
from app.services.finops import record_replay_usage

router = APIRouter(prefix="/v1", tags=["agent"])


@router.post("/runs/{run_id}/replay", response_model=ReplayAttemptRecord)
async def post_run_replay(
    run_id: str,
    payload: ReplayRunRequest,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "operator", "admin")
    run = await get_run(session, run_id)
    if run is None:
        raise await run_missing(session, run_id, auth_context)
    _query_tenant(run.tenant_id, auth_context)
    plan = await get_execution_plan_for_run(session, run_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Execution plan not found.")
    steps = await list_execution_plan_steps(session, run_id)
    if payload.sandbox_rerun:
        attempt = await persist_sandbox_replay(
            session, get_settings(), run=run, plan=plan, steps=steps, mode=payload.mode, created_by=payload.created_by, requested_by=_actor(auth_context)
        )
    else:
        attempt = await persist_replay_attempt(session, run=run, plan=plan, steps=steps, mode=payload.mode, created_by=payload.created_by)
    await record_replay_usage(session, run=run, plan=plan, attempt=attempt)
    return ReplayAttemptRecord(**replay_attempt_to_dict(attempt))
