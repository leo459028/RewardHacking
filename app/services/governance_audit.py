"""GovernanceEvent persistence helper (v1.6.0: extracted from ``app.services.audit_log``).

A dependency-light leaf module, so evaluation and judge code can write audit events without importing
``audit_log`` (which reaches the FinOps lease code). ``audit_log.persist_governance_event`` re-exports it.

v1.6.1 P5: importing this module imports ``app.services.audit_chain``, whose session hooks append an audit chain
entry for every new ``GovernanceEvent`` in the same transaction, just before commit (plan D31).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.governance_event import GovernanceEvent
from app.services import audit_chain  # noqa: F401  (registers the same-transaction chain hooks)


def add_governance_event(
    session: AsyncSession,
    *,
    tenant_id: str,
    user_id: str = "",
    run_id: str = "",
    event_type: str,
    decision: str,
    reason: str,
    limit_value: int = 0,
    used: int = 0,
    remaining: int = 0,
    details: dict[str, Any] | None = None,
) -> GovernanceEvent:
    """v1.6.1: add an event to the caller's transaction without committing (the caller commits with its own rows)."""
    event = GovernanceEvent(
        tenant_id=tenant_id,
        user_id=user_id,
        run_id=run_id,
        event_type=event_type,
        decision=decision,
        reason=reason,
        limit_value=limit_value,
        used=used,
        remaining=remaining,
        details=details or {},
        created_at=audit_chain.utcnow(),
    )
    session.add(event)
    return event


async def persist_governance_event(
    session: AsyncSession,
    *,
    tenant_id: str,
    user_id: str = "",
    run_id: str = "",
    event_type: str,
    decision: str,
    reason: str,
    limit_value: int = 0,
    used: int = 0,
    remaining: int = 0,
    details: dict[str, Any] | None = None,
) -> GovernanceEvent:
    event = add_governance_event(
        session,
        tenant_id=tenant_id,
        user_id=user_id,
        run_id=run_id,
        event_type=event_type,
        decision=decision,
        reason=reason,
        limit_value=limit_value,
        used=used,
        remaining=remaining,
        details=details,
    )
    await session.commit()
    await session.refresh(event)
    return event
