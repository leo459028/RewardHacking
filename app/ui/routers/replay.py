"""Dashboard dry-run replay (moved from ``app/ui/routes.py`` in v1.6.2 under the router split rule; the path is unchanged)."""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.infra.postgres import get_session
from app.services.audit_log import get_run
from app.services.checkpoint import persist_replay_attempt, persist_sandbox_replay
from app.services.control_plane import get_execution_plan_for_run, list_execution_plan_steps
from app.services.finops import record_replay_usage
from app.services.verification.errors import VerificationError
from app.ui.routes import dashboard_auth
from app.ui.security import actor_for, csrf_protect, ensure_tenant_access, needs

router = APIRouter(tags=["dashboard"], dependencies=[Depends(dashboard_auth), Depends(csrf_protect)])


@router.post("/dashboard/runs/{run_id}/replay", response_class=HTMLResponse, dependencies=needs("runs.replay"))
async def dashboard_run_replay(
    request: Request,
    run_id: str,
    session: AsyncSession = Depends(get_session),
):
    run = await get_run(session, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    await ensure_tenant_access(request, session, run.tenant_id, permission="runs.replay")
    plan = await get_execution_plan_for_run(session, run_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Execution plan not found.")
    steps = await list_execution_plan_steps(session, run_id)
    actor = actor_for(request, "dashboard-operator")
    form = await request.form()  # read here, so the v1.5.12 OpenAPI baseline of this route stays as it is
    if form.get("sandbox_rerun"):
        try:
            attempt = await persist_sandbox_replay(
                session, get_settings(), run=run, plan=plan, steps=steps, mode="dry_run", created_by=actor, requested_by=actor
            )
        except VerificationError as exc:
            return RedirectResponse(f"/dashboard/runs/{quote(run_id, safe='')}?error={quote(exc.code)}", status_code=303)
        await record_replay_usage(session, run=run, plan=plan, attempt=attempt)
        message = f"replay_{attempt.status}:{attempt.result_payload['verification_id']}"
        return RedirectResponse(f"/dashboard/runs/{quote(run_id, safe='')}?message={quote(message)}", status_code=303)
    attempt = await persist_replay_attempt(session, run=run, plan=plan, steps=steps, mode="dry_run", created_by=actor)
    await record_replay_usage(session, run=run, plan=plan, attempt=attempt)
    return HTMLResponse(
        f'<meta http-equiv="refresh" content="0; url=/dashboard/runs/{run_id}">'
        f'<a href="/dashboard/runs/{run_id}">Back to run detail</a>'
    )
