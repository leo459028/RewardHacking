"""v1.6.0 Dashboard: audit retention (dry-run plans, receipts, tombstones, legal holds, FinOps ledger report).

Creating a dry-run plan and creating a legal hold are allowed here (same-origin ``Origin`` / ``Referer`` check).

v1.6.1 P7 (plan D44, feature ten): applying a plan and releasing a hold from the Dashboard, **``session`` mode
only**. Both routes declare their permission (``retention.apply`` / ``legal_hold.release``, admin, step-up
re-authentication) and pass ``csrf_protect``; ``off`` mode (no signed-in user) refuses them with 403
``dashboard_auth_off`` and the page keeps showing the API command. Apply also needs the plan's ``purge_id`` typed as
the confirmation string; the service still refuses unless ``AUDIT_RETENTION_PURGE_MODE=apply``. Both go through the
dual-control enforcement point ``change_requests.execute_or_request()`` (D39): with the action listed in
``DUAL_CONTROL_ACTIONS`` the form stores a change request instead and the page links to it.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_state_store
from app.core.config import get_settings
from app.infra.postgres import get_session
from app.services import change_requests, retention
from app.services.access.permissions import has_permission, view_allowed
from app.services.run_state import RunStateStore
from app.ui.routers.change_requests import pending_message
from app.ui.routers.judges import same_origin
from app.ui.routes import dashboard_auth, templates
from app.ui.security import actor_for, csrf_protect, needs, principal_of

router = APIRouter(tags=["dashboard"], dependencies=[Depends(dashboard_auth), Depends(csrf_protect)])

PAGE = "/dashboard/retention"
DASHBOARD_ACTOR = "dashboard"
HOLD_TABS = ("active", "released")


def _redirect(**params: str) -> RedirectResponse:
    query = urlencode({key: value for key, value in params.items() if value})
    return RedirectResponse(f"{PAGE}?{query}" if query else PAGE, status_code=303)


def _forbidden() -> HTMLResponse:
    return HTMLResponse("Cross-origin form submission refused (Origin / Referer must match this host).", status_code=403)


def _auth_off(permission: str) -> JSONResponse:
    """D44: ``off`` mode has no apply / release from the Dashboard (there is no signed-in person to record)."""
    return JSONResponse(
        status_code=403,
        content={
            "error": {
                "code": "dashboard_auth_off",
                "permission": permission,
                "message": "Dashboard sign-in is off (DASHBOARD_AUTH_MODE=off); use the API or switch to session mode.",
            }
        },
    )


def _receipt_message(purge: Any) -> str:
    return (
        f"Purge {purge.purge_id} {purge.status}: {int((purge.applied_counts or {}).get('runs', 0))} runs purged, "
        f"{len(purge.skipped_at_apply or [])} skipped, {len(purge.failures or [])} failures, "
        f"receipt {purge.receipt_digest or '-'}."
    )


@router.get("/dashboard/retention", response_class=HTMLResponse, dependencies=needs("retention.view", view="retention"))
async def dashboard_retention(request: Request, session: AsyncSession = Depends(get_session)):
    settings = get_settings()
    purge_id = request.query_params.get("purge_id", "")
    hold_tab = request.query_params.get("holds", "active")
    if hold_tab not in HOLD_TABS:
        hold_tab = "active"
    purges = await retention.list_purges(session, limit=20)
    selected = await retention.get_purge(session, purge_id) if purge_id else None
    if selected is None:
        selected = next((item for item in purges if item.status == "planned"), purges[0] if purges else None)
    plan: dict[str, Any] | None = retention.purge_to_dict(selected) if selected is not None else None
    holds = await retention.list_holds(session, status=hold_tab, limit=100)
    tombstones = await retention.list_tombstones(session, limit=20)
    principal = principal_of(request)
    signed_in = not principal.synthetic

    def allowed(permission: str) -> bool:
        return signed_in and has_permission(principal.role, permission) and view_allowed(principal.tenant_id, "retention")

    change_request = None
    change_id = request.query_params.get("change_id", "")
    if change_id and signed_in:
        cr = await change_requests.get_change_request(session, change_id)
        if cr is not None and (principal.is_platform or (cr.tenant_id or "") == principal.tenant_id):
            change_request = change_requests.to_dict(cr)
    return templates.TemplateResponse(
        request,
        "retention.html",
        {
            "policy": retention.policy_view(settings),
            "plan": plan,
            "purges": [retention.purge_to_dict(item) for item in purges],
            "holds": [retention.hold_to_dict(item) for item in holds],
            "hold_tab": hold_tab,
            "tombstones": [retention.tombstone_summary(item) for item in tombstones],
            "ledger": await retention.ledger_report(session, settings),
            "message": request.query_params.get("message", ""),
            "error": request.query_params.get("error", ""),
            "active": "retention",
            # v1.6.1 P7 (D44): the forms replace the API command in session mode only.
            "signed_in": signed_in,
            "can_apply": allowed("retention.apply"),
            "can_release": allowed("legal_hold.release"),
            "dual_apply": change_requests.is_enabled(settings, change_requests.RETENTION_APPLY),
            "dual_release": change_requests.is_enabled(settings, change_requests.LEGAL_HOLD_RELEASE),
            "change_request": change_request,
        },
    )


@router.post("/dashboard/retention/plans", response_class=HTMLResponse, dependencies=needs("retention.plan", view="retention"))
async def dashboard_retention_plan(request: Request, session: AsyncSession = Depends(get_session)):
    """Read-only dry-run report (D11). Never deletes anything."""
    if not same_origin(request):
        return _forbidden()
    try:
        purge = await retention.build_plan(session, get_settings(), actor=actor_for(request, DASHBOARD_ACTOR))
    except retention.RetentionError as exc:
        return _redirect(error=f"{exc.code}: {exc.message}")
    return _redirect(purge_id=purge.purge_id, message=f"Dry-run report {purge.purge_id} created.")


@router.post("/dashboard/retention/holds", response_class=HTMLResponse, dependencies=needs("legal_hold.create", view="retention"))
async def dashboard_retention_hold_create(request: Request, session: AsyncSession = Depends(get_session)):
    """Create a legal hold as the signed-in user (off mode: local admin)."""
    if not same_origin(request):
        return _forbidden()
    form = await request.form()
    try:
        hold = await retention.create_hold(
            session,
            scope_type=str(form.get("scope_type", "")),
            scope_id=str(form.get("scope_id", "")),
            reason=str(form.get("reason", "")),
            actor=actor_for(request, DASHBOARD_ACTOR),
            actor_role=principal_of(request).role,  # v1.6.1 D23: the signed-in role (off mode: local admin)
            actor_tenant_id=principal_of(request).tenant_id,
        )
    except retention.RetentionError as exc:
        return _redirect(error=f"{exc.code}: {exc.message}")
    return _redirect(message=f"Legal hold {hold.hold_id} created for {hold.scope_type} {hold.scope_id}.")


@router.post(
    "/dashboard/retention/plans/{purge_id}/apply", response_class=HTMLResponse, dependencies=needs("retention.apply", view="retention")
)
async def dashboard_retention_apply(
    request: Request,
    purge_id: str,
    session: AsyncSession = Depends(get_session),
    state_store: RunStateStore = Depends(get_state_store),
):
    """D44: apply a dry-run plan (admin, step-up, CSRF, the ``purge_id`` typed as confirmation), or store a change
    request when ``retention.apply`` is under dual control. The service keeps refusing unless the purge mode is
    ``apply``; its error code is shown on the page and nothing is deleted."""
    principal = principal_of(request)
    if principal.synthetic:
        return _auth_off("retention.apply")
    if not same_origin(request):
        return _forbidden()
    form = await request.form()
    if str(form.get("confirm_purge_id", "")).strip() != purge_id:
        return _redirect(
            purge_id=purge_id,
            error="retention_confirm_mismatch: type the purge ID exactly to confirm; nothing was deleted.",
        )
    try:
        outcome = await change_requests.execute_or_request(
            session,
            get_settings(),
            change_requests.RETENTION_APPLY,
            {"purge_id": purge_id, "plan_digest": str(form.get("plan_digest", "")), "confirm": True},
            change_requests.Requester.from_principal(principal, actor_for(request, DASHBOARD_ACTOR)),
            reason=str(form.get("reason", "")),
            state_store=state_store,
        )
    except (retention.RetentionError, change_requests.ChangeRequestError) as exc:
        return _redirect(purge_id=purge_id, error=f"{exc.code}: {exc.message}")
    if outcome.pending:
        assert outcome.change_request is not None
        return _redirect(purge_id=purge_id, change_id=outcome.change_request.change_id, message=pending_message(outcome))
    return _redirect(purge_id=purge_id, message=_receipt_message(outcome.result))


@router.post(
    "/dashboard/retention/holds/{hold_id}/release",
    response_class=HTMLResponse,
    dependencies=needs("legal_hold.release", view="retention"),
)
async def dashboard_retention_hold_release(request: Request, hold_id: str, session: AsyncSession = Depends(get_session)):
    """D44: release a legal hold (admin, step-up, CSRF, a required reason), or store a change request when
    ``legal_hold.release`` is under dual control."""
    principal = principal_of(request)
    if principal.synthetic:
        return _auth_off("legal_hold.release")
    if not same_origin(request):
        return _forbidden()
    form = await request.form()
    release_reason = str(form.get("release_reason", ""))
    try:
        outcome = await change_requests.execute_or_request(
            session,
            get_settings(),
            change_requests.LEGAL_HOLD_RELEASE,
            {"hold_id": hold_id, "release_reason": release_reason},
            change_requests.Requester.from_principal(principal, actor_for(request, DASHBOARD_ACTOR)),
            reason=release_reason,
        )
    except (retention.RetentionError, change_requests.ChangeRequestError) as exc:
        return _redirect(error=f"{exc.code}: {exc.message}")
    if outcome.pending:
        assert outcome.change_request is not None
        return _redirect(change_id=outcome.change_request.change_id, message=pending_message(outcome))
    return _redirect(holds="released", message=f"Legal hold {hold_id} released.")
