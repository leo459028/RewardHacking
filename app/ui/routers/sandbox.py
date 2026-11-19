"""v1.6.2 Dashboard: sandbox runtime (`/dashboard/sandbox`, plan D39-D41).

Platform view for ``admin``, ``operator`` and ``auditor``: backend, runtime, the execution gate and its reasons,
conformance history, runner heartbeats and recent jobs. ``admin`` can queue a conformance run. The feature flag and the
runtime are environment settings and cannot be changed here.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_state_store
from app.core.config import get_settings
from app.infra.postgres import get_session
from app.services.run_state import RunStateStore
from app.services.sandbox import jobs
from app.services.sandbox.registry import DOCKER, configured_backend
from app.ui.routes import dashboard_auth, templates
from app.ui.security import actor_for, csrf_protect, needs

router = APIRouter(tags=["dashboard"], dependencies=[Depends(dashboard_auth), Depends(csrf_protect)])

PAGE = "/dashboard/sandbox"
VIEW = "sandbox"


@router.get(PAGE, response_class=HTMLResponse, dependencies=needs("sandbox.view", view=VIEW))
async def dashboard_sandbox(
    request: Request,
    session: AsyncSession = Depends(get_session),
    state_store: RunStateStore = Depends(get_state_store),
):
    context = await jobs.overview(session, get_settings(), state_store)
    return templates.TemplateResponse(
        request,
        "sandbox.html",
        {
            **context,
            "active": "sandbox",
            "message": request.query_params.get("message", ""),
            "error": request.query_params.get("error", ""),
        },
    )


@router.post(PAGE + "/conformance", response_class=HTMLResponse, dependencies=needs("sandbox.manage", view=VIEW))
async def dashboard_sandbox_conformance(request: Request, session: AsyncSession = Depends(get_session)):
    settings = get_settings()
    if configured_backend(settings) != DOCKER or not settings.sandbox_execution_enabled:
        return RedirectResponse(f"{PAGE}?error=sandbox_execution_disabled", status_code=303)
    job, created = await jobs.enqueue_conformance(session, requested_by=actor_for(request, "dashboard"))
    await session.commit()
    message = "conformance_queued" if created else "conformance_already_active"
    return RedirectResponse(f"{PAGE}?message={message}:{job.job_id}", status_code=303)
