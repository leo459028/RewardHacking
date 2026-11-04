from __future__ import annotations

from datetime import datetime, timezone

from app.core.config import Settings
from app.observability.metrics import QUOTA_DECISIONS_TOTAL
from app.schemas.agent import AgentRequest
from app.services.run_state import QuotaResult, RunStateStore


def _minute_bucket() -> str:
    return datetime.now(timezone.utc).strftime("requests:%Y%m%d%H%M")


def _hour_bucket() -> str:
    return datetime.now(timezone.utc).strftime("high_risk:%Y%m%d%H")


def _user_minute_bucket(user_id: str) -> str:
    safe_user = (user_id or "anonymous").strip() or "anonymous"
    return datetime.now(timezone.utc).strftime(f"user:{safe_user}:requests:%Y%m%d%H%M")


def _tool_minute_bucket(tool_name: str) -> str:
    safe_tool = (tool_name or "unknown").strip() or "unknown"
    return datetime.now(timezone.utc).strftime(f"tool:{safe_tool}:calls:%Y%m%d%H%M")


async def check_request_quota(
    state_store: RunStateStore,
    *,
    settings: Settings,
    tenant_id: str,
    limit: int | None = None,
) -> QuotaResult:
    res = await state_store.increment_quota(
        tenant_id,
        _minute_bucket(),
        limit=limit if limit is not None else settings.tenant_request_limit_per_minute,
        ttl_seconds=60,
    )
    QUOTA_DECISIONS_TOTAL.labels(
        tenant_id=tenant_id,
        scope="tenant",
        decision="allowed" if res.allowed else "blocked",
    ).inc()
    return res


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
    tenant_id = request.tenant_id or "default"
    res = await state_store.increment_quota(
        tenant_id,
        _hour_bucket(),
        limit=limit if limit is not None else settings.tenant_high_risk_limit_per_hour,
        ttl_seconds=3600,
    )
    QUOTA_DECISIONS_TOTAL.labels(
        tenant_id=tenant_id,
        scope="high_risk",
        decision="allowed" if res.allowed else "blocked",
    ).inc()
    return res


async def check_user_request_quota(
    state_store: RunStateStore,
    *,
    settings: Settings,
    tenant_id: str,
    user_id: str,
    limit: int | None = None,
) -> QuotaResult:
    res = await state_store.increment_quota(
        tenant_id,
        _user_minute_bucket(user_id),
        limit=limit if limit is not None else settings.user_request_limit_per_minute,
        ttl_seconds=60,
    )
    QUOTA_DECISIONS_TOTAL.labels(
        tenant_id=tenant_id,
        scope="user",
        decision="allowed" if res.allowed else "blocked",
    ).inc()
    return res


async def check_tool_quotas(
    state_store: RunStateStore,
    *,
    settings: Settings,
    tenant_id: str,
    user_id: str,
    allowed_tools: list[str],
    tool_limits: dict[str, int] | None = None,
) -> tuple[bool, str, QuotaResult | None]:
    limits = tool_limits or {}
    for tool_name in dict.fromkeys(allowed_tools or []):
        eff_limit = int(limits.get(tool_name) or settings.tool_call_limit_per_minute)
        res = await state_store.increment_quota(
            tenant_id,
            _tool_minute_bucket(tool_name),
            limit=eff_limit,
            ttl_seconds=60,
        )
        QUOTA_DECISIONS_TOTAL.labels(
            tenant_id=tenant_id,
            scope="tool",
            decision="allowed" if res.allowed else "blocked",
        ).inc()
        if not res.allowed:
            return False, tool_name, res
    return True, "", None
