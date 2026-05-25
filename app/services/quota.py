from __future__ import annotations

from datetime import datetime, timezone

from app.core.config import Settings
from app.schemas.agent import AgentRequest
from app.services.run_state import QuotaResult, RunStateStore


def _minute_bucket() -> str:
    return datetime.now(timezone.utc).strftime("requests:%Y%m%d%H%M")


def _hour_bucket() -> str:
    return datetime.now(timezone.utc).strftime("high_risk:%Y%m%d%H")


async def check_request_quota(
    state_store: RunStateStore,
    *,
    settings: Settings,
    tenant_id: str,
    limit: int | None = None,
) -> QuotaResult:
    return await state_store.increment_quota(
        tenant_id,
        _minute_bucket(),
        limit=limit if limit is not None else settings.tenant_request_limit_per_minute,
        ttl_seconds=60,
    )


async def check_high_risk_quota(
    state_store: RunStateStore,
    *,
    settings: Settings,
    request: AgentRequest,
    risk_score: float,
    limit: int | None = None,
) -> QuotaResult | None:
    if risk_score < settings.default_risk_threshold:
        return None
    return await state_store.increment_quota(
        request.tenant_id or "default",
        _hour_bucket(),
        limit=limit if limit is not None else settings.tenant_high_risk_limit_per_hour,
        ttl_seconds=3600,
    )
