"""Dashboard: Governance (tenants, API keys, governance events).

v1.6.3 P2: moved from ``app/ui/routes.py`` under the Router Split Rule (``AGENTS.md`` §2.1); paths, permissions,
forms and pages are unchanged. New in v1.6.3 (plan D8): the API key table shows who created each key
(``app.services.api_key_provenance``), and a notice above it counts active keys an environment API key created,
the same number as the ``env_created_api_keys`` operations check (D7). Seeing the column follows the existing
``api_keys.view`` permission (admin, read-only auditor); revoking still needs ``api_keys.manage``.
"""

from __future__ import annotations

import uuid
from typing import Any, Sequence

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.infra.postgres import get_session
from app.models.tenant import TenantApiKey
from app.services import api_key_provenance
from app.services.access.permissions import PLATFORM_VIEW, has_permission
from app.services.access.principal import Principal
from app.services.audit_log import (
    create_tenant,
    create_tenant_api_key,
    get_tenant,
    get_tenant_api_key,
    governance_summary,
    list_governance_events,
    list_runs,
    list_tenant_api_keys,
    list_tenants,
    persist_governance_event,
    revoke_tenant_api_key,
    update_tenant,
)
from app.services.auth import generate_api_key, hash_api_key
from app.ui.routes import dashboard_auth, templates
from app.ui.security import actor_for, csrf_protect, ensure_permission, needs, principal_of, scoped_tenant

router = APIRouter(tags=["dashboard"], dependencies=[Depends(dashboard_auth), Depends(csrf_protect)])


async def _key_provenance_context(
    session: AsyncSession, principal: Principal, api_keys: Sequence[TenantApiKey]
) -> dict[str, Any]:
    """D8: creators for the listed keys and the D7 notice (platform users: every tenant; tenant-bound: their own)."""
    if not has_permission(principal.role, "api_keys.view"):
        return {"api_key_creators": {}, "env_created_keys": []}
    creators = await api_key_provenance.creators_for_keys(session, api_keys)
    env_keys = await api_key_provenance.active_env_created_keys(
        session, tenant_id=None if principal.is_platform else principal.tenant_id
    )
    return {
        "api_key_creators": creators,
        "env_created_keys": [key.key_id for key in env_keys],
        "env_key_label": api_key_provenance.ENV_LABEL,
        "runbook_section": api_key_provenance.RUNBOOK_SECTION,
        "runbook_url": api_key_provenance.RUNBOOK_URL,
    }


@router.get("/dashboard/governance", response_class=HTMLResponse, dependencies=needs("dashboard.view", view="governance"))
async def dashboard_governance(
    request: Request,
    tenant_id: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    principal = principal_of(request)
    tenant_id = await scoped_tenant(request, session, tenant_id)
    runs = await list_runs(session, tenant_id=tenant_id or None, limit=100)
    tenants = await list_tenants(session)
    if not principal.is_platform:
        tenants = [tenant for tenant in tenants if tenant.tenant_id == principal.tenant_id]
    tenant_ids = [tenant.tenant_id for tenant in tenants]
    if not tenant_ids:
        tenant_ids = sorted({run.tenant_id for run in await list_runs(session, tenant_id=tenant_id or None, limit=200)})
    selected_tenant = tenant_id or (tenant_ids[0] if tenant_ids else "default")
    summary = await governance_summary(session, tenant_id=selected_tenant)
    events = await list_governance_events(session, tenant_id=tenant_id or None, limit=25)
    # API parity: listing API keys is admin only (``api_keys.view`` adds the read-only auditor, D21).
    api_keys = await list_tenant_api_keys(session, tenant_id=selected_tenant) if has_permission(principal.role, "api_keys.view") else []
    from app.services.review_queue import get_evaluation_policy, policy_to_dict
    from app.ui.routers.reviews import list_evaluation_policies

    evaluation_policies = [
        policy for policy in await list_evaluation_policies(session) if principal.is_platform or policy.tenant_id == principal.tenant_id
    ]
    selected_policy = policy_to_dict(selected_tenant, await get_evaluation_policy(session, selected_tenant))
    return templates.TemplateResponse(
        request,
        "governance.html",
        {
            "evaluation_policies": evaluation_policies,
            "selected_policy": selected_policy,
            "governance_message": request.query_params.get("message", ""),
            "summary": summary,
            "events": events,
            "runs": runs,
            "tenants": tenants,
            "api_keys": api_keys,
            "tenant_ids": tenant_ids,
            "tenant_id": tenant_id or "",
            "selected_tenant": selected_tenant,
            "newly_created_key": "",
            "newly_created_key_name": "",
            "active": "governance",
            **await _key_provenance_context(session, principal, api_keys),
        },
    )


@router.post("/dashboard/governance/tenants", response_class=HTMLResponse, dependencies=needs("tenants.manage", view=PLATFORM_VIEW))
async def dashboard_governance_tenant_upsert(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    form = await request.form()
    tenant_id = str(form.get("tenant_id", "default")).strip() or "default"
    display_name = str(form.get("display_name", "")).strip() or tenant_id
    status = str(form.get("status", "active")).strip() or "active"
    try:
        req_limit = max(1, int(str(form.get("request_limit_per_minute", "60")).strip() or "60"))
    except ValueError:
        req_limit = 60
    try:
        hr_limit = max(1, int(str(form.get("high_risk_limit_per_hour", "10")).strip() or "10"))
    except ValueError:
        hr_limit = 10
    if status != "active":
        await ensure_permission(request, session, "tenants.disable")

    existing = await get_tenant(session, tenant_id)
    if existing is None:
        await create_tenant(
            session,
            tenant_id=tenant_id,
            display_name=display_name,
            request_limit_per_minute=req_limit,
            high_risk_limit_per_hour=hr_limit,
        )
        if status != "active":
            created_tenant = await get_tenant(session, tenant_id)
            await update_tenant(
                session,
                created_tenant,
                display_name=display_name,
                status=status,
                request_limit_per_minute=req_limit,
                high_risk_limit_per_hour=hr_limit,
            )
        await persist_governance_event(
            session,
            tenant_id=tenant_id,
            user_id=actor_for(request, "dashboard-admin"),
            event_type="tenant.created",
            decision="created",
            reason=f"Tenant {tenant_id} created from Governance Dashboard.",
            limit_value=req_limit,
            used=0,
            remaining=req_limit,
            details={"high_risk_limit_per_hour": hr_limit, "status": status},
        )
    else:
        await update_tenant(
            session,
            existing,
            display_name=display_name,
            status=status,
            request_limit_per_minute=req_limit,
            high_risk_limit_per_hour=hr_limit,
        )
        await persist_governance_event(
            session,
            tenant_id=tenant_id,
            user_id=actor_for(request, "dashboard-admin"),
            event_type="tenant.updated",
            decision="updated",
            reason=f"Tenant {tenant_id} updated from Governance Dashboard.",
            limit_value=req_limit,
            used=0,
            remaining=req_limit,
            details={"high_risk_limit_per_hour": hr_limit, "status": status},
        )
    return RedirectResponse(f"/dashboard/governance?tenant_id={tenant_id}", status_code=303)


async def _issue_tenant_api_key_and_render(
    request: Request,
    session: AsyncSession,
    *,
    tenant_id: str,
    name: str,
    role: str,
) -> HTMLResponse:
    settings = get_settings()
    existing_tenant = await get_tenant(session, tenant_id)
    if existing_tenant is None:
        await create_tenant(
            session,
            tenant_id=tenant_id,
            display_name=tenant_id,
            request_limit_per_minute=settings.tenant_request_limit_per_minute,
            high_risk_limit_per_hour=settings.tenant_high_risk_limit_per_hour,
        )
    raw_key = generate_api_key()
    key_prefix = raw_key[:8]
    key_hash = hash_api_key(raw_key)
    key_id = f"key_{uuid.uuid4().hex[:12]}"
    record = await create_tenant_api_key(
        session,
        tenant_id=tenant_id,
        key_id=key_id,
        name=name,
        key_prefix=key_prefix,
        key_hash=key_hash,
        role=role,
    )
    await persist_governance_event(
        session,
        tenant_id=tenant_id,
        user_id=actor_for(request, "dashboard-admin"),
        event_type="api_key.created",
        decision="created",
        reason=f"API key {name} ({role}) issued for tenant {tenant_id}.",
        details={"key_id": record.key_id, "role": role, "key_prefix": key_prefix},
    )
    runs = await list_runs(session, tenant_id=tenant_id, limit=100)
    tenants = await list_tenants(session)
    tenant_ids = [t.tenant_id for t in tenants]
    summary = await governance_summary(session, tenant_id=tenant_id)
    events = await list_governance_events(session, tenant_id=tenant_id, limit=25)
    api_keys = await list_tenant_api_keys(session, tenant_id=tenant_id)
    from app.services.review_queue import get_evaluation_policy, policy_to_dict
    from app.ui.routers.reviews import list_evaluation_policies

    response = templates.TemplateResponse(
        request,
        "governance.html",
        {
            "evaluation_policies": await list_evaluation_policies(session),
            "selected_policy": policy_to_dict(tenant_id, await get_evaluation_policy(session, tenant_id)),
            "governance_message": "",
            "summary": summary,
            "events": events,
            "runs": runs,
            "tenants": tenants,
            "api_keys": api_keys,
            "tenant_ids": tenant_ids,
            "tenant_id": tenant_id,
            "selected_tenant": tenant_id,
            "newly_created_key": raw_key,
            "newly_created_key_name": name,
            "active": "governance",
            **await _key_provenance_context(session, principal_of(request), api_keys),
        },
        status_code=200,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/dashboard/governance/api-keys", response_class=HTMLResponse, dependencies=needs("api_keys.manage", view=PLATFORM_VIEW))
async def dashboard_governance_api_key_create_form(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    form = await request.form()
    tenant_id = str(form.get("tenant_id", "default")).strip() or "default"
    name = str(form.get("name", "dashboard-key")).strip() or "dashboard-key"
    role = str(form.get("role", "operator")).strip() or "operator"
    if role not in {"admin", "operator", "viewer", "finops_admin", "reviewer", "auditor"}:
        role = "operator"
    return await _issue_tenant_api_key_and_render(
        request,
        session,
        tenant_id=tenant_id,
        name=name,
        role=role,
    )


@router.post("/dashboard/governance/tenants/{tenant_id}/api-keys", response_class=HTMLResponse, dependencies=needs("api_keys.manage", view=PLATFORM_VIEW))
async def dashboard_governance_api_key_create(
    tenant_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    form = await request.form()
    eff_tenant = str(form.get("tenant_id", "")).strip() or tenant_id or "default"
    name = str(form.get("name", "dashboard-key")).strip() or "dashboard-key"
    role = str(form.get("role", "operator")).strip() or "operator"
    if role not in {"admin", "operator", "viewer", "finops_admin", "reviewer", "auditor"}:
        role = "operator"
    return await _issue_tenant_api_key_and_render(
        request,
        session,
        tenant_id=eff_tenant,
        name=name,
        role=role,
    )


@router.post("/dashboard/governance/tenants/{tenant_id}/api-keys/{key_id}/revoke", response_class=HTMLResponse, dependencies=needs("api_keys.manage", view=PLATFORM_VIEW))
async def dashboard_governance_api_key_revoke(
    request: Request,
    tenant_id: str,
    key_id: str,
    session: AsyncSession = Depends(get_session),
):
    api_key = await get_tenant_api_key(session, tenant_id=tenant_id, key_id=key_id)
    if api_key is not None:
        revoked = await revoke_tenant_api_key(session, api_key)
        await persist_governance_event(
            session,
            tenant_id=tenant_id,
            user_id=actor_for(request, "dashboard-admin"),
            event_type="api_key.revoked",
            decision="revoked",
            reason=f"API key {revoked.name} (id={revoked.key_id}) revoked from Governance Dashboard.",
            details={"key_id": revoked.key_id, "key_prefix": revoked.key_prefix},
        )
    return RedirectResponse(f"/dashboard/governance?tenant_id={tenant_id}", status_code=303)
