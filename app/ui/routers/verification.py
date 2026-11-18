"""v1.6.2 Dashboard: verification suites and platform re-runs (`/dashboard/verification`, plan D39, D40).

Tenant-bound users only see and change their own tenant. A new suite changes the grading standard, so managing suites
needs a recent re-authentication. Results are monitor-only and never change a verdict.
"""

from __future__ import annotations

import re
import shlex
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.infra.postgres import get_session
from app.models.evaluation_review import TenantEvaluationPolicy
from app.services import change_requests
from app.services.audit_log import get_run
from app.services.sandbox import docker_config, gate
from app.services.verification import publishing, sampling, suites
from app.services.verification.errors import VerificationError
from app.ui.routers.change_requests import pending_message
from app.ui.routes import dashboard_auth, templates
from app.ui.security import actor_for, csrf_protect, ensure_tenant_access, needs, principal_of, scoped_tenant

router = APIRouter(tags=["dashboard"], dependencies=[Depends(dashboard_auth), Depends(csrf_protect)])

PAGE = "/dashboard/verification"
VIEW = "verification"
MANAGE = "verification_suites.manage"


async def _hide_from_tenant_users(request: Request, session: AsyncSession, permission: str) -> None:
    """An unknown ID answers tenant-bound users like another tenant's ID, so IDs cannot be probed."""
    await ensure_tenant_access(request, session, None, permission=permission)


def _back(*, message: str = "", error: str = "") -> RedirectResponse:
    query = f"message={quote(message)}" if message else f"error={quote(error)}"
    return RedirectResponse(f"{PAGE}?{query}", status_code=303)


async def _default_suites(session: AsyncSession, tenant_id: str | None) -> set[str]:
    stmt = select(TenantEvaluationPolicy.default_verification_suite_id).where(TenantEvaluationPolicy.default_verification_suite_id != "")
    if tenant_id:
        stmt = stmt.where(TenantEvaluationPolicy.tenant_id == tenant_id)
    return set((await session.scalars(stmt)).all())


@router.get(PAGE, response_class=HTMLResponse, dependencies=needs("verification.view", view=VIEW))
async def dashboard_verification(request: Request, session: AsyncSession = Depends(get_session)):
    settings = get_settings()
    tenant_id = await scoped_tenant(request, session, request.query_params.get("tenant_id"), permission="verification.view")
    rows = await suites.list_suites(session, tenant_id=tenant_id)
    runs = await sampling.list_verifications(session, tenant_id=tenant_id, limit=50)
    return templates.TemplateResponse(
        request,
        "verification.html",
        {
            "active": "verification",
            "suites": [suites.suite_to_dict(row) for row in rows],
            "default_suites": await _default_suites(session, tenant_id),
            "runs": [sampling.verification_to_dict(row) for row in runs],
            "tenant_id": tenant_id or "",
            "tenant_locked": not principal_of(request).is_platform,
            "gate": (await gate.evaluate(session, settings)).to_dict(),
            "verify_mode": sampling.verify_mode(settings),
            "daily_cap": int(settings.sandbox_verify_daily_cap_per_tenant),
            "images": sorted(docker_config.sandbox_images(settings)),
            "max_upload_mb": int(settings.sandbox_artifact_max_bytes) // 1_048_576,
            "message": request.query_params.get("message", ""),
            "error": request.query_params.get("error", ""),
        },
    )


@router.get(PAGE + "/runs/{verification_id}", response_class=HTMLResponse, dependencies=needs("verification.view", view=VIEW))
async def dashboard_verification_detail(verification_id: str, request: Request, session: AsyncSession = Depends(get_session)):
    row = await sampling.get_verification(session, verification_id)
    if row is None:
        await _hide_from_tenant_users(request, session, "verification.view")
        raise HTTPException(status_code=404, detail="Verification not found.")
    await ensure_tenant_access(request, session, row.tenant_id, permission="verification.view")
    return templates.TemplateResponse(
        request, "verification_detail.html", {"active": "verification", "item": sampling.verification_to_dict(row)}
    )


@router.post(PAGE + "/suites", response_class=HTMLResponse, dependencies=needs(MANAGE, view=VIEW))
async def dashboard_verification_upload(request: Request, session: AsyncSession = Depends(get_session)):
    form = await request.form()
    upload = form.get("file")
    principal = principal_of(request)
    tenant_id = principal.tenant_id if not principal.is_platform else (str(form.get("tenant_id") or "").strip() or "default")
    if upload is None or isinstance(upload, str):
        return _back(error="artifact_invalid_archive")
    settings = get_settings()
    try:
        outcome = await publishing.upload(
            session,
            settings,
            data=await upload.read(int(settings.sandbox_artifact_max_bytes) + 1),  # one byte over is enough to refuse it
            tenant_id=tenant_id,
            name=str(form.get("name") or "").strip(),
            base_ref=str(form.get("base_ref") or "").strip(),
            image_name=str(form.get("image_name") or "python"),
            test_command=shlex.split(str(form.get("test_command") or "")) or None,
            test_paths=[item for item in re.split(r"[\s,]+", str(form.get("test_paths") or "")) if item] or None,
            requester=change_requests.Requester.from_principal(principal, actor_for(request, "dashboard")),
        )
    except (VerificationError, change_requests.ChangeRequestError) as exc:
        return _back(error=exc.code)
    except ValueError:
        return _back(error="suite_test_command_invalid")
    if outcome.pending:
        return _back(message=pending_message(outcome))
    return _back(message=f"suite_published:{outcome.result.suite_id}")


async def _managed_suite(request: Request, session: AsyncSession, suite_id: str):
    suite = await suites.get_suite(session, suite_id)
    if suite is None:
        await _hide_from_tenant_users(request, session, MANAGE)
        raise HTTPException(status_code=404, detail="Suite not found.")
    await ensure_tenant_access(request, session, suite.tenant_id, permission=MANAGE)
    return suite


@router.post(PAGE + "/suites/{suite_id}/disable", response_class=HTMLResponse, dependencies=needs(MANAGE, view=VIEW))
async def dashboard_verification_disable(suite_id: str, request: Request, session: AsyncSession = Depends(get_session)):
    suite = await _managed_suite(request, session, suite_id)
    try:
        await suites.disable(session, suite, actor=actor_for(request, "dashboard"))
    except VerificationError as exc:
        return _back(error=exc.code)
    return _back(message=f"suite_disabled:{suite_id}")


@router.post(PAGE + "/suites/{suite_id}/default", response_class=HTMLResponse, dependencies=needs(MANAGE, view=VIEW))
async def dashboard_verification_default(
    suite_id: str, request: Request, enabled: str = Form(default="1"), session: AsyncSession = Depends(get_session)
):
    suite = await _managed_suite(request, session, suite_id)
    try:
        await suites.set_default(session, suite, enabled=enabled == "1", actor=actor_for(request, "dashboard"))
    except VerificationError as exc:
        return _back(error=exc.code)
    return _back(message=f"suite_default_{'set' if enabled == '1' else 'cleared'}:{suite_id}")


@router.post(PAGE + "/request", response_class=HTMLResponse, dependencies=needs("verification.request", view=VIEW))
async def dashboard_verification_request(request: Request, run_id: str = Form(default=""), session: AsyncSession = Depends(get_session)):
    run = await get_run(session, run_id.strip())
    if run is None:
        await _hide_from_tenant_users(request, session, "verification.request")
        return _back(error="run_not_found")
    await ensure_tenant_access(request, session, run.tenant_id, permission="verification.request")
    try:
        row, created = await sampling.request_manual(session, get_settings(), run=run, requested_by=actor_for(request, "dashboard"))
    except VerificationError as exc:
        return _back(error=exc.code)
    state = row.skip_reason if row.status == sampling.SKIPPED else ("queued" if created else "already_active")
    return _back(message=f"verification_{state}:{row.verification_id}")
