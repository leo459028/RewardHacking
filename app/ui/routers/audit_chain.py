"""v1.6.1 Dashboard: audit chain (`/dashboard/audit-chain`, plan D33-D36, feature eight).

Shows the chain head, entry counts, the genesis entry, recent entries and checkpoints (signature status, ``key_id``).
**Verify** runs the bounded online verification (``AUDIT_CHAIN_VERIFY_MAX_ENTRIES``; full verification is the CLI)
and writes nothing. **Create checkpoint** (``admin``, step-up re-authentication) signs the current head; the daily
job does the same. Platform view: ``admin`` and ``auditor`` only (``audit_chain.read`` / ``.verify``).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.infra.postgres import get_session
from app.services import audit_chain
from app.ui.routes import dashboard_auth, templates
from app.ui.security import actor_for, csrf_protect, needs

router = APIRouter(tags=["dashboard"], dependencies=[Depends(dashboard_auth), Depends(csrf_protect)])

PAGE = "/dashboard/audit-chain"
VIEW = "audit_chain"


async def _render(request: Request, session: AsyncSession, *, verify_result: dict[str, Any] | None = None) -> HTMLResponse:
    settings = get_settings()
    entries = await audit_chain.list_entries(session, limit=25)
    checkpoints = await audit_chain.list_checkpoints(session, limit=20)
    return templates.TemplateResponse(
        request,
        "audit_chain.html",
        {
            "active": "audit-chain",
            "status": await audit_chain.status(session, settings),
            "entries": [audit_chain.entry_to_dict(item) for item in entries],
            "checkpoints": [audit_chain.checkpoint_to_dict(item) for item in checkpoints],
            "verify_result": verify_result,
            "message": request.query_params.get("message", ""),
            "error": request.query_params.get("error", ""),
        },
    )


@router.get(PAGE, response_class=HTMLResponse, dependencies=needs("audit_chain.read", view=VIEW))
async def dashboard_audit_chain(request: Request, session: AsyncSession = Depends(get_session)):
    return await _render(request, session)


@router.post(PAGE + "/verify", response_class=HTMLResponse, dependencies=needs("audit_chain.verify", view=VIEW))
async def dashboard_audit_chain_verify(request: Request, session: AsyncSession = Depends(get_session)):
    form = await request.form()
    raw = str(form.get("from_seq", "") or "").strip()
    from_seq = int(raw) if raw.isdigit() and int(raw) > 0 else None
    settings = get_settings()
    result = await audit_chain.verify(session, settings, from_seq=from_seq, max_entries=int(settings.audit_chain_verify_max_entries))
    return await _render(request, session, verify_result=result)


@router.post(PAGE + "/checkpoints", response_class=HTMLResponse, dependencies=needs("audit_chain.checkpoint", view=VIEW))
async def dashboard_audit_chain_checkpoint(request: Request, session: AsyncSession = Depends(get_session)):
    try:
        result = await audit_chain.create_checkpoint(session, get_settings(), actor=actor_for(request, "dashboard"))
    except audit_chain.AuditChainError as exc:
        await session.rollback()
        return RedirectResponse(f"{PAGE}?error={exc.code}", status_code=303)
    if not result.created:
        return RedirectResponse(f"{PAGE}?message={result.reason}", status_code=303)
    return RedirectResponse(f"{PAGE}?message=checkpoint_created:{result.checkpoint.checkpoint_id}", status_code=303)
