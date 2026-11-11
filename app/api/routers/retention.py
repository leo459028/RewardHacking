"""v1.6.0 audit retention and legal hold API (D11-D17).

The service layer (``app/services/retention.py``) enforces every rule: purge mode, plan digest / TTL / confirm,
re-check at apply, and legal hold roles. This router only maps the principal (``actor``, ``actor_role``,
``actor_tenant_id``) and renders ``RetentionError`` as ``{"error": {"code": ...}}`` (handler in ``app/main.py``).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.common import _actor, _query_tenant
from app.api.deps import get_state_store
from app.core.config import get_settings
from app.infra.postgres import get_session
from app.schemas.retention import (
    LedgerReportResponse,
    LegalHoldCreateRequest,
    LegalHoldRecord,
    LegalHoldReleaseRequest,
    RetentionApplyRequest,
    RetentionPolicyResponse,
    RetentionPurgeRecord,
    RetentionTombstoneRecord,
)
from app.services import retention
from app.services.auth import AuthContext, get_auth_context, require_role
from app.services.run_state import RunStateStore

router = APIRouter(prefix="/v1", tags=["retention"])

READ_ROLES = ("operator", "admin")


def hold_actor_role(auth_context: AuthContext) -> str:
    """Map the API principal to the role the service checks (D15). Auth disabled = local admin (dev only)."""
    if not auth_context.enabled:
        return "admin"
    if auth_context.role == "super_admin":
        return "admin"
    return auth_context.role


def _not_found(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=404, detail={"code": code, "message": message})


@router.get("/retention/policy", response_model=RetentionPolicyResponse)
async def get_retention_policy(auth_context: AuthContext = Depends(get_auth_context)):
    require_role(auth_context, "viewer", "operator", "admin")
    return retention.policy_view(get_settings())


@router.post("/retention/plans", response_model=RetentionPurgeRecord)
async def post_retention_plan(
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "admin")
    purge = await retention.build_plan(session, get_settings(), actor=_actor(auth_context))
    return retention.purge_to_dict(purge)


@router.get("/retention/plans/{purge_id}", response_model=RetentionPurgeRecord)
async def get_retention_plan(
    purge_id: str,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, *READ_ROLES)
    purge = await retention.get_purge(session, purge_id)
    if purge is None:
        raise _not_found("retention_plan_not_found", "Retention plan not found.")
    return retention.purge_to_dict(purge)


@router.post("/retention/plans/{purge_id}/apply", response_model=RetentionPurgeRecord)
async def post_retention_apply(
    purge_id: str,
    payload: RetentionApplyRequest,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
    state_store: RunStateStore = Depends(get_state_store),
):
    require_role(auth_context, "admin")
    purge = await retention.apply_plan(
        session,
        get_settings(),
        purge_id,
        plan_digest=payload.plan_digest,
        confirm=payload.confirm,
        actor=_actor(auth_context),
        state_store=state_store,
    )
    return retention.purge_to_dict(purge)


@router.get("/retention/purges", response_model=list[RetentionPurgeRecord])
async def get_retention_purges(
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, *READ_ROLES)
    return [retention.purge_to_dict(item) for item in await retention.list_purges(session, status=status, limit=limit)]


@router.get("/retention/tombstones", response_model=list[RetentionTombstoneRecord])
async def get_retention_tombstones(
    tenant_id: str | None = None,
    purge_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "viewer", "operator", "admin")
    tenant_id = _query_tenant(tenant_id, auth_context)
    rows = await retention.list_tombstones(session, tenant_id=tenant_id, purge_id=purge_id, limit=limit)
    return [retention.tombstone_summary(item) for item in rows]


@router.get("/retention/finops-ledger-report", response_model=LedgerReportResponse)
async def get_finops_ledger_report(
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "viewer", "operator", "admin", "finops_admin")
    return await retention.ledger_report(session, get_settings())


@router.post("/legal-holds", response_model=LegalHoldRecord)
async def post_legal_hold(
    payload: LegalHoldCreateRequest,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    hold = await retention.create_hold(
        session,
        scope_type=payload.scope_type,
        scope_id=payload.scope_id,
        reason=payload.reason,
        actor=_actor(auth_context),
        actor_role=hold_actor_role(auth_context),
        actor_tenant_id=auth_context.tenant_id,
    )
    return retention.hold_to_dict(hold)


@router.get("/legal-holds", response_model=list[LegalHoldRecord])
async def get_legal_holds(
    tenant_id: str | None = None,
    status: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "viewer", "operator", "admin")
    tenant_id = _query_tenant(tenant_id, auth_context)
    rows = await retention.list_holds(session, tenant_id=tenant_id, status=status, limit=limit)
    return [retention.hold_to_dict(item) for item in rows]


@router.post("/legal-holds/{hold_id}/release", response_model=LegalHoldRecord)
async def post_release_legal_hold(
    hold_id: str,
    payload: LegalHoldReleaseRequest,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    hold = await retention.release_hold(
        session,
        hold_id,
        actor=_actor(auth_context),
        actor_role=hold_actor_role(auth_context),
        release_reason=payload.release_reason,
    )
    return retention.hold_to_dict(hold)
