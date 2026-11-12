"""v1.6.1 audit chain API (plan D33-D35, feature eight).

Read-only: status, entries, checkpoints and a bounded online verification (``AUDIT_CHAIN_VERIFY_MAX_ENTRIES`` per
call; page with ``from_seq`` / ``next_from_seq``, full verification is the CLI ``python -m app.jobs.audit_chain
--verify``). Responses carry hashes and identifiers only, never event content. Roles: ``admin`` and the read-only
``auditor`` key role (D21). Checkpoints are written by the job or the Dashboard, not by this API.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.infra.postgres import get_session
from app.schemas.audit_chain import AuditChainVerifyRequest
from app.services import audit_chain
from app.services.auth import AuthContext, get_auth_context, require_role

router = APIRouter(prefix="/v1", tags=["audit-chain"])

READ_ROLES = ("admin", "auditor")


@router.get("/audit-chain/status")
async def get_audit_chain_status(
    auth_context: AuthContext = Depends(get_auth_context), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    require_role(auth_context, *READ_ROLES)
    return {**await audit_chain.status(session, get_settings()), "signal_usage": "monitor_only"}


@router.get("/audit-chain/entries")
async def list_audit_chain_entries(
    from_seq: int | None = Query(default=None, ge=1),
    entry_type: str | None = Query(default=None, max_length=32),
    run_id: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=100, ge=1, le=500),
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    require_role(auth_context, *READ_ROLES)
    rows = await audit_chain.list_entries(session, from_seq=from_seq, entry_type=entry_type, run_id=run_id, limit=limit)
    return {"entries": [audit_chain.entry_to_dict(row) for row in rows], "count": len(rows)}


@router.get("/audit-chain/checkpoints")
async def list_audit_chain_checkpoints(
    limit: int = Query(default=50, ge=1, le=500),
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    require_role(auth_context, *READ_ROLES)
    rows = await audit_chain.list_checkpoints(session, limit=limit)
    return {"checkpoints": [audit_chain.checkpoint_to_dict(row) for row in rows], "count": len(rows)}


@router.post("/audit-chain/verify")
async def verify_audit_chain(
    payload: AuditChainVerifyRequest | None = None,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Re-computes hashes and compares sources; writes nothing. At most ``AUDIT_CHAIN_VERIFY_MAX_ENTRIES`` entries."""
    require_role(auth_context, *READ_ROLES)
    settings = get_settings()
    body = payload or AuditChainVerifyRequest()
    result = await audit_chain.verify(
        session, settings, from_seq=body.from_seq, to_seq=body.to_seq, max_entries=int(settings.audit_chain_verify_max_entries)
    )
    return {**result, "signal_usage": "monitor_only"}
