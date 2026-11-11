"""v1.6.1 Dashboard: Change Requests | 變更覆核 (`/dashboard/change-requests`, plan D37-D43, feature nine).

Lists change requests (tenant-bound users see their own tenant's only) and lets a **second, signed-in** person
approve or reject a pending one (``change_requests.decide``, step-up re-authentication) and the requester cancel it
(``change_requests.cancel``). The service re-checks every D40 rule, so a hidden button is never the only guard:
self-approval, an approver without the action's own permission, another tenant's request and ``off`` mode (no
signed-in user) are refused with 403 ``{"error": {"code": ...}}``. Approval executes the stored action at once.
Named "change request", never approval or review (AGENTS.md 2.3 Approval != Review).
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_state_store
from app.core.config import get_settings
from app.infra.postgres import get_session
from app.services import change_requests
from app.services.access.permissions import has_permission
from app.services.run_state import RunStateStore
from app.ui.routes import dashboard_auth, templates
from app.ui.security import actor_for, csrf_protect, ensure_tenant_access, needs, principal_of, scoped_tenant

router = APIRouter(tags=["dashboard"], dependencies=[Depends(dashboard_auth), Depends(csrf_protect)])

PAGE = "/dashboard/change-requests"
VIEW = "change_requests"


def _redirect(*, message: str = "", error: str = "", status: str = "") -> RedirectResponse:
    query = []
    if status:
        query.append(f"status={quote(status)}")
    if message:
        query.append(f"message={quote(message)}")
    if error:
        query.append(f"error={quote(error)}")
    return RedirectResponse(PAGE + ("?" + "&".join(query) if query else ""), status_code=303)


@router.get(PAGE, response_class=HTMLResponse, dependencies=needs("dashboard.view", view=VIEW))
async def dashboard_change_requests(request: Request, session: AsyncSession = Depends(get_session)):
    settings = get_settings()
    principal = principal_of(request)
    tenant_id = await scoped_tenant(request, session, request.query_params.get("tenant_id"))
    status = request.query_params.get("status", "")
    status = status if status in change_requests.STATUSES else ""
    await change_requests.expire_stale(session, settings)
    rows = await change_requests.list_change_requests(session, tenant_id=tenant_id, status=status or None, limit=200)
    items = []
    for row in rows:
        item = change_requests.to_dict(row)
        own = (not principal.synthetic) and change_requests.is_requester(
            row, change_requests.Requester.from_principal(principal, principal.actor)
        )
        item["own"] = own
        item["can_decide"] = (
            row.status == change_requests.PENDING
            and not principal.synthetic
            and not own
            and has_permission(principal.role, "change_requests.decide")
            and has_permission(principal.role, change_requests.ACTION_PERMISSIONS[row.action])
            and (principal.is_platform or (row.tenant_id or "") == principal.tenant_id)
        )
        item["can_cancel"] = row.status == change_requests.PENDING and own and has_permission(principal.role, "change_requests.cancel")
        items.append(item)
    enabled, unknown = change_requests.configured_actions(settings)
    return templates.TemplateResponse(
        request,
        "change_requests.html",
        {
            "active": "change-requests",
            "items": items,
            "status_filter": status,
            "statuses": change_requests.STATUSES,
            "tenant_id": tenant_id or "",
            "enabled_actions": sorted(enabled),
            "unknown_actions": unknown,
            "all_actions": change_requests.ACTIONS,
            "action_labels": change_requests.ACTION_LABELS,
            "ttl_hours": settings.dual_control_request_ttl_hours,
            "sign_in_off": principal.synthetic,
            "message": request.query_params.get("message", ""),
            "error": request.query_params.get("error", ""),
        },
    )


async def _tenant_guard(request: Request, session: AsyncSession, change_id: str, permission: str) -> None:
    cr = await change_requests.get_change_request(session, change_id)
    if cr is None:
        raise change_requests.ChangeRequestError("change_request_not_found", "Change request not found.", 404)
    # D22 (b): a tenant-bound user never learns about another tenant's request (platform requests included).
    await ensure_tenant_access(request, session, cr.tenant_id, permission=permission)


def _refused_or_redirect(exc: change_requests.ChangeRequestError) -> RedirectResponse:
    """403 refusals keep the JSON error shape (handler in ``app/main.py``); state conflicts go back to the page."""
    if exc.status_code == 403:
        raise exc
    return _redirect(error=f"{exc.code}: {exc.message}")


@router.post(PAGE + "/{change_id}/approve", response_class=HTMLResponse, dependencies=needs("change_requests.decide", view=VIEW))
async def dashboard_change_request_approve(
    request: Request,
    change_id: str,
    session: AsyncSession = Depends(get_session),
    state_store: RunStateStore = Depends(get_state_store),
):
    form = await request.form()
    await _tenant_guard(request, session, change_id, "change_requests.decide")
    try:
        cr = await change_requests.approve(
            session, get_settings(), change_id, principal_of(request), reason=str(form.get("reason", "")), state_store=state_store
        )
    except change_requests.ChangeRequestError as exc:
        return _refused_or_redirect(exc)
    if cr.status == change_requests.EXECUTED:
        return _redirect(message=f"{change_id} approved and executed.")
    return _redirect(error=f"{change_id} approved but execution failed: {cr.error_code}. The target was not changed by this request.")


@router.post(PAGE + "/{change_id}/reject", response_class=HTMLResponse, dependencies=needs("change_requests.decide", view=VIEW))
async def dashboard_change_request_reject(request: Request, change_id: str, session: AsyncSession = Depends(get_session)):
    form = await request.form()
    await _tenant_guard(request, session, change_id, "change_requests.decide")
    try:
        await change_requests.reject(session, get_settings(), change_id, principal_of(request), reason=str(form.get("reason", "")))
    except change_requests.ChangeRequestError as exc:
        return _refused_or_redirect(exc)
    return _redirect(message=f"{change_id} rejected; nothing was executed.")


@router.post(PAGE + "/{change_id}/cancel", response_class=HTMLResponse, dependencies=needs("change_requests.cancel", view=VIEW))
async def dashboard_change_request_cancel(request: Request, change_id: str, session: AsyncSession = Depends(get_session)):
    form = await request.form()
    await _tenant_guard(request, session, change_id, "change_requests.cancel")
    principal = principal_of(request)
    requester = change_requests.Requester.from_principal(principal, actor_for(request, "dashboard"))
    try:
        await change_requests.cancel(session, get_settings(), change_id, requester, reason=str(form.get("reason", "")))
    except change_requests.ChangeRequestError as exc:
        return _refused_or_redirect(exc)
    return _redirect(message=f"{change_id} cancelled; nothing was executed.")


def pending_message(outcome: change_requests.Outcome) -> str:
    """Shared Dashboard message for an action that became a change request (used by the four guarded forms)."""
    cr = outcome.change_request
    assert cr is not None
    verb = "submitted" if outcome.created else "already pending"
    return (
        f"Change request {cr.change_id} {verb} | 已送出變更覆核: a second person must approve it on {PAGE} before it takes effect "
        f"(expires {change_requests.to_dict(cr)['expires_at']})."
    )
