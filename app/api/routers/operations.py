"""Operations and observability endpoints (v1.6.0 D27: moved from ``app/api/routes.py``; paths unchanged)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.common import _actor, _trace_id
from app.api.deps import get_state_store
from app.core import tracing
from app.core.config import get_settings
from app.infra.postgres import get_session
from app.schemas.audit import (
    OperationsCheckRecord,
    OperationsEvidenceCreateRequest,
    OperationsEvidenceRecord,
    OperationsReadinessResponse,
    ReleaseGateCreateRequest,
    ReleaseGateItemRecord,
    ReleaseGateRecord,
)
from app.services.audit_log import observability_summary
from app.services.auth import AuthContext, auth_error, get_auth_context, require_role
from app.services.operations import (
    compute_operations_readiness,
    get_release_gate,
    list_operations_evidence,
    list_release_gate_items,
    list_release_gates,
    persist_operations_evidence,
    persist_release_gate,
)
from app.services.run_state import RunStateStore

router = APIRouter(prefix="/v1", tags=["operations"])


def _evidence_record(evidence) -> OperationsEvidenceRecord:
    return OperationsEvidenceRecord(
        evidence_type=evidence.evidence_type,
        status=evidence.status,
        summary=evidence.summary,
        reference_url=evidence.reference_url,
        metadata=evidence.metadata_payload,
        created_by=evidence.created_by,
        created_at=evidence.created_at,
    )


def _check_record(check) -> OperationsCheckRecord:
    return OperationsCheckRecord(
        check_key=check.check_key,
        status=check.status,
        title=check.title,
        detail=check.detail,
        metadata=check.metadata,
    )


def _gate_item_record(item) -> ReleaseGateItemRecord:
    return ReleaseGateItemRecord(
        check_key=item.check_key,
        status=item.status,
        title=item.title,
        detail=item.detail,
        metadata=item.metadata_payload,
    )


async def _gate_record(session: AsyncSession, gate) -> ReleaseGateRecord:
    items = await list_release_gate_items(session, gate.gate_id)
    return ReleaseGateRecord(
        gate_id=gate.gate_id,
        release_version=gate.release_version,
        status=gate.status,
        summary=gate.summary,
        created_by=gate.created_by,
        trace_id=gate.trace_id,
        created_at=gate.created_at,
        items=[_gate_item_record(item) for item in items],
    )


@router.get("/operations/readiness", response_model=OperationsReadinessResponse)
async def get_operations_readiness(
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
    state_store: RunStateStore = Depends(get_state_store),
):
    require_role(auth_context, "viewer", "operator", "admin")
    readiness = await compute_operations_readiness(session, settings=get_settings(), state_store=state_store)
    return OperationsReadinessResponse(
        status=readiness.status,
        summary=readiness.summary,
        checks=[_check_record(check) for check in readiness.checks],
    )


@router.post("/operations/evidence", response_model=OperationsEvidenceRecord)
async def post_operations_evidence(
    payload: OperationsEvidenceCreateRequest,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "admin")
    if payload.status not in {"pass", "warn", "fail"}:
        raise auth_error(422, "invalid_evidence_status", "Evidence status must be pass, warn, or fail.")
    evidence = await persist_operations_evidence(
        session,
        evidence_type=payload.evidence_type,
        status=payload.status,
        summary=payload.summary,
        reference_url=payload.reference_url,
        metadata=payload.metadata,
        created_by=_actor(auth_context),
    )
    return _evidence_record(evidence)


@router.get("/operations/evidence", response_model=list[OperationsEvidenceRecord])
async def get_operations_evidence(
    evidence_type: str | None = None,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "viewer", "operator", "admin")
    evidence = await list_operations_evidence(session, evidence_type=evidence_type)
    return [_evidence_record(item) for item in evidence]


@router.post("/operations/release-gates", response_model=ReleaseGateRecord)
async def post_release_gate(
    payload: ReleaseGateCreateRequest,
    request: Request,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
    state_store: RunStateStore = Depends(get_state_store),
):
    require_role(auth_context, "admin")
    readiness = await compute_operations_readiness(session, settings=get_settings(), state_store=state_store)
    gate = await persist_release_gate(
        session,
        release_version=payload.release_version,
        created_by=payload.created_by,
        trace_id=_trace_id(request),
        readiness=readiness,
    )
    return await _gate_record(session, gate)


@router.get("/operations/release-gates", response_model=list[ReleaseGateRecord])
async def get_release_gates(
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "viewer", "operator", "admin")
    gates = await list_release_gates(session)
    return [await _gate_record(session, gate) for gate in gates]


@router.get("/operations/release-gates/{gate_id}", response_model=ReleaseGateRecord)
async def get_release_gate_by_id(
    gate_id: str,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "viewer", "operator", "admin")
    gate = await get_release_gate(session, gate_id)
    if gate is None:
        raise HTTPException(status_code=404, detail="Release gate not found.")
    return await _gate_record(session, gate)


@router.get("/observability/summary")
async def get_observability_summary(
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "viewer", "operator", "admin")
    summary = await observability_summary(session)
    return {
        "worker": summary.worker,
        "incidents": summary.incidents,
        "approvals": summary.approvals,
        "evaluator": summary.evaluator,
        "slo": summary.slo,
        "finops": summary.finops,
    }


@router.get("/observability/tracing")
async def get_observability_tracing(auth_context: AuthContext = Depends(get_auth_context)):
    """v1.6.0: W3C propagation / OTel export status. Exporter host only, never the full endpoint or headers."""
    require_role(auth_context, "viewer", "operator", "admin")
    return tracing.status(get_settings())
