"""v1.6.0 Dashboard: audit retention (dry-run plans, receipts, tombstones, legal holds, FinOps ledger report).

Creating a dry-run plan and creating a legal hold are allowed here (same-origin ``Origin`` / ``Referer`` check).
Applying a plan and releasing a hold are **API only** until Dashboard RBAC / CSRF ship in v1.6.1: this router
has no route for either, and the page shows the equivalent API command instead.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.infra.postgres import get_session
from app.services import retention
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
    """Create a legal hold as the signed-in user (off mode: local admin); releasing a hold stays API-only until P7."""
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
