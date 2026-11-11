"""Operations and observability Dashboard views (v1.6.0 D27: moved from ``app/ui/routes.py``; paths unchanged).

v1.6.0 adds the Tracing panel on ``/dashboard/observability``; the ``retention_policy`` and
``sandbox_backend_health`` readiness checks appear on ``/dashboard/operations`` through the shared check list.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_state_store
from app.core import tracing
from app.core.config import get_settings
from app.infra.postgres import get_session
from app.services.audit_log import observability_summary
from app.services.operations import (
    compute_operations_readiness,
    list_operations_evidence,
    list_release_gate_items,
    list_release_gates,
    persist_operations_evidence,
    persist_release_gate,
)
from app.services.run_state import RunStateStore
from app.ui.routes import dashboard_auth, templates

router = APIRouter(tags=["dashboard"], dependencies=[Depends(dashboard_auth)])


@router.get("/dashboard/observability", response_class=HTMLResponse)
async def dashboard_observability(request: Request, session: AsyncSession = Depends(get_session)):
    summary = await observability_summary(session)
    return templates.TemplateResponse(
        request,
        "observability.html",
        {
            "summary": summary,
            "tracing": tracing.status(get_settings()),
            "active": "observability",
        },
    )


@router.get("/dashboard/operations", response_class=HTMLResponse)
async def dashboard_operations(
    request: Request,
    session: AsyncSession = Depends(get_session),
    state_store: RunStateStore = Depends(get_state_store),
):
    readiness = await compute_operations_readiness(session, settings=get_settings(), state_store=state_store)
    evidence = await list_operations_evidence(session, limit=25)
    gates = await list_release_gates(session, limit=10)
    gate_items = {}
    for gate in gates:
        gate_items[gate.gate_id] = await list_release_gate_items(session, gate.gate_id)
    return templates.TemplateResponse(
        request,
        "operations.html",
        {
            "readiness": readiness,
            "evidence": evidence,
            "gates": gates,
            "gate_items": gate_items,
            "active": "operations",
        },
    )


@router.post("/dashboard/operations/evidence", response_class=HTMLResponse)
async def dashboard_operations_evidence(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    form = await request.form()
    await persist_operations_evidence(
        session,
        evidence_type=str(form.get("evidence_type", "")),
        status=str(form.get("status", "pass")),
        summary=str(form.get("summary", "")),
        reference_url=str(form.get("reference_url", "")),
        metadata={"source": "dashboard"},
        created_by="dashboard",
    )
    return HTMLResponse(
        '<meta http-equiv="refresh" content="0; url=/dashboard/operations">'
        '<a href="/dashboard/operations">Back to operations</a>'
    )


@router.post("/dashboard/operations/release-gates", response_class=HTMLResponse)
async def dashboard_operations_release_gate(
    request: Request,
    session: AsyncSession = Depends(get_session),
    state_store: RunStateStore = Depends(get_state_store),
):
    form = await request.form()
    readiness = await compute_operations_readiness(session, settings=get_settings(), state_store=state_store)
    await persist_release_gate(
        session,
        release_version=str(form.get("release_version", "v1.6.0")),
        created_by=str(form.get("created_by", "dashboard")),
        trace_id=str(getattr(request.state, "trace_id", "")),
        readiness=readiness,
    )
    return HTMLResponse(
        '<meta http-equiv="refresh" content="0; url=/dashboard/operations">'
        '<a href="/dashboard/operations">Back to operations</a>'
    )
