"""Budget policy and violation CRUD, period bounds and policy cost matching."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.finops import (
    BudgetPolicy,
    BudgetViolation,
    CostEvent,
)
from app.schemas.finops import (
    BudgetPolicyCreateRequest,
    BudgetPolicyUpdateRequest,
)
from app.services.finops.core import (
    _ensure_utc,
    _money,
    _value,
)
from app.services.finops.settlement import (
    _cost_in_period,
    _cost_mcp_servers,
    _cost_tools,
)


async def create_budget_policy(
    session: AsyncSession,
    payload: BudgetPolicyCreateRequest,
    *,
    supersede_existing: bool = False,
) -> BudgetPolicy:
    if supersede_existing:
        existing_rows = list(
            (
                await session.scalars(
                    select(BudgetPolicy).where(
                        BudgetPolicy.tenant_id == (payload.tenant_id or "default"),
                        BudgetPolicy.scope == payload.scope,
                        BudgetPolicy.scope_id == payload.scope_id,
                        BudgetPolicy.period == str(_value(payload.period)),
                        BudgetPolicy.is_active.is_(True),
                    )
                )
            ).all()
        )
        now = datetime.now(timezone.utc)
        for old_row in existing_rows:
            old_row.is_active = False
            old_row.effective_to = now
    has_hard = payload.hard_limit_amount is not None or payload.limit_amount is not None
    if has_hard:
        hard_limit = payload.hard_limit_amount if payload.hard_limit_amount is not None else payload.limit_amount
        limit_amount = payload.limit_amount if payload.limit_amount is not None else hard_limit
        soft_limit = payload.soft_limit_amount if payload.soft_limit_amount is not None else Decimal("0")
    else:
        soft_limit = payload.soft_limit_amount or Decimal("0")
        hard_limit = Decimal("0")
        limit_amount = soft_limit

    if "actions" in payload.model_fields_set and "actions_on_hard_limit" not in payload.model_fields_set:
        hard_actions = [str(_value(item)) for item in payload.actions]
    else:
        hard_actions = [str(_value(item)) for item in payload.actions_on_hard_limit]

    item = BudgetPolicy(
        budget_id=payload.budget_id or f"budget_{uuid4().hex}",
        tenant_id=payload.tenant_id or "default",
        scope=payload.scope,
        scope_id=payload.scope_id,
        project_id=payload.project_id,
        environment=payload.environment,
        policy_name=payload.policy_name,
        policy_version=payload.policy_version,
        period=str(_value(payload.period)),
        period_start=payload.period_start,
        period_end=payload.period_end,
        timezone=payload.timezone,
        limit_amount=_money(limit_amount),
        soft_limit_amount=_money(soft_limit),
        hard_limit_amount=_money(hard_limit),
        currency=payload.currency,
        base_currency=payload.base_currency,
        resource_type=payload.resource_type,
        provider=payload.provider,
        model_name=payload.model_name,
        model_tier=payload.model_tier,
        tool_id=payload.tool_id,
        workflow_id=payload.workflow_id,
        budget_basis=str(_value(payload.budget_basis)),
        include_failed_runs=payload.include_failed_runs,
        include_replay_runs=payload.include_replay_runs,
        include_dev_environment=payload.include_dev_environment,
        actions=[str(_value(item)) for item in payload.actions],
        actions_on_soft_limit=[str(_value(item)) for item in payload.actions_on_soft_limit],
        actions_on_hard_limit=hard_actions,
        notification_channels=payload.notification_channels,
        approval_required=payload.approval_required,
        approver_role=payload.approver_role,
        fallback_model_tier=payload.fallback_model_tier,
        effective_from=payload.effective_from,
        effective_to=payload.effective_to,
        created_by=payload.created_by,
        updated_by=payload.updated_by,
        is_active=payload.is_active,
        metadata_payload=payload.metadata,
    )
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item


async def get_budget_policy(session: AsyncSession, budget_id: str) -> BudgetPolicy | None:
    return await session.scalar(select(BudgetPolicy).where(BudgetPolicy.budget_id == budget_id))


async def disable_budget_policy(
    session: AsyncSession,
    budget_id: str,
    *,
    updated_by: str = "",
) -> BudgetPolicy | None:
    item = await get_budget_policy(session, budget_id)
    if item is None:
        return None
    item.is_active = False
    item.effective_to = datetime.now(timezone.utc)
    if updated_by:
        item.updated_by = updated_by
    await session.commit()
    await session.refresh(item)
    return item


async def update_budget_policy(
    session: AsyncSession,
    budget_id: str,
    payload: BudgetPolicyUpdateRequest,
) -> BudgetPolicy | None:
    item = await session.scalar(select(BudgetPolicy).where(BudgetPolicy.budget_id == budget_id))
    if item is None:
        return None
    updates = payload.model_dump(exclude_unset=True)
    for key, value in updates.items():
        if key == "metadata":
            item.metadata_payload = value or {}
        elif isinstance(value, Enum):
            setattr(item, key, value.value)
        elif key in {"limit_amount", "soft_limit_amount", "hard_limit_amount"} and value is not None:
            setattr(item, key, _money(value))
        elif key in {"actions", "actions_on_soft_limit", "actions_on_hard_limit"} and value is not None:
            setattr(item, key, [str(_value(entry)) for entry in value])
        elif value is not None:
            setattr(item, key, value)
    await session.commit()
    await session.refresh(item)
    return item


async def list_budget_policies(
    session: AsyncSession,
    *,
    tenant_id: str | None = None,
    active_only: bool = False,
) -> list[BudgetPolicy]:
    stmt = select(BudgetPolicy).order_by(BudgetPolicy.created_at.desc())
    if tenant_id:
        stmt = stmt.where(BudgetPolicy.tenant_id == tenant_id)
    if active_only:
        stmt = stmt.where(BudgetPolicy.is_active.is_(True))
    return list((await session.scalars(stmt)).all())


async def list_budget_violations(
    session: AsyncSession,
    *,
    tenant_id: str | None = None,
    run_id: str | None = None,
    resolution_status: str | None = None,
    limit: int = 100,
) -> list[BudgetViolation]:
    stmt = select(BudgetViolation).order_by(BudgetViolation.created_at.desc()).limit(limit)
    if tenant_id:
        stmt = stmt.where(BudgetViolation.tenant_id == tenant_id)
    if run_id:
        stmt = stmt.where(BudgetViolation.run_id == run_id)
    if resolution_status:
        stmt = stmt.where(BudgetViolation.resolution_status == resolution_status)
    return list((await session.scalars(stmt)).all())


async def get_budget_violation(session: AsyncSession, violation_id: str) -> BudgetViolation | None:
    return await session.scalar(select(BudgetViolation).where(BudgetViolation.violation_id == violation_id))


async def acknowledge_budget_violation(
    session: AsyncSession,
    violation_id: str,
    *,
    acknowledged_by: str = "",
    resolved_by: str = "",
    note: str = "",
    notes: str = "",
) -> BudgetViolation | None:
    item = await get_budget_violation(session, violation_id)
    if item is None:
        return None
    actor = acknowledged_by or resolved_by or "operator"
    final_note = note or notes
    item.resolution_status = "acknowledged"
    item.acknowledged_by = actor
    item.acknowledged_at = datetime.now(timezone.utc)
    if final_note:
        details = dict(item.details or {})
        details["acknowledge_note"] = final_note
        item.details = details
    await session.commit()
    await session.refresh(item)
    return item


async def resolve_budget_violation(
    session: AsyncSession,
    violation_id: str,
    *,
    resolved_by: str = "",
    resolution_status: str = "resolved",
    resolution_note: str = "",
    note: str = "",
    notes: str = "",
) -> BudgetViolation | None:
    item = await get_budget_violation(session, violation_id)
    if item is None:
        return None
    now = datetime.now(timezone.utc)
    actor = resolved_by or "admin"
    final_note = resolution_note or note or notes
    item.resolution_status = resolution_status if resolution_status in {"resolved", "ignored"} else "resolved"
    item.resolved_by = actor
    item.resolved_at = now
    if not item.acknowledged_by:
        item.acknowledged_by = actor
        item.acknowledged_at = now
    details = dict(item.details or {})
    if final_note:
        details["resolution_note"] = final_note
    item.details = details
    await session.commit()
    await session.refresh(item)
    return item


def _resolve_period_bounds(
    policy: BudgetPolicy,
    now: datetime,
) -> tuple[datetime, datetime, Decimal] | None:
    if policy.scope == "run" or policy.period == "per_run":
        return None
    now_utc = _ensure_utc(now) or datetime.now(timezone.utc)
    p_start = _ensure_utc(policy.period_start)
    p_end = _ensure_utc(policy.period_end)
    if p_start is not None and p_end is not None and p_end > p_start:
        total_hours = Decimal(str(max(1.0, (p_end - p_start).total_seconds() / 3600.0)))
        return p_start, p_end, total_hours

    if policy.period == "daily":
        start = datetime(now_utc.year, now_utc.month, now_utc.day, tzinfo=timezone.utc)
        end = start + timedelta(days=1)
    elif policy.period == "weekly":
        monday = (now_utc - timedelta(days=now_utc.weekday())).date()
        start = datetime(monday.year, monday.month, monday.day, tzinfo=timezone.utc)
        end = start + timedelta(days=7)
    elif policy.period == "quarterly":
        q_month = ((now_utc.month - 1) // 3) * 3 + 1
        start = datetime(now_utc.year, q_month, 1, tzinfo=timezone.utc)
        end = (
            datetime(now_utc.year + 1, 1, 1, tzinfo=timezone.utc)
            if q_month == 10
            else datetime(now_utc.year, q_month + 3, 1, tzinfo=timezone.utc)
        )
    elif policy.period == "yearly":
        start = datetime(now_utc.year, 1, 1, tzinfo=timezone.utc)
        end = datetime(now_utc.year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        start = datetime(now_utc.year, now_utc.month, 1, tzinfo=timezone.utc)
        end = (
            datetime(now_utc.year + 1, 1, 1, tzinfo=timezone.utc)
            if now_utc.month == 12
            else datetime(now_utc.year, now_utc.month + 1, 1, tzinfo=timezone.utc)
        )

    if p_start is not None and p_start > start:
        start = p_start
    if p_end is not None and p_end < end and p_end > start:
        end = p_end
    total_hours = Decimal(str(max(1.0, (end - start).total_seconds() / 3600.0)))
    return start, end, total_hours


def _matching_costs_for_policy(
    tenant_costs: list[CostEvent],
    policy: BudgetPolicy,
    now: datetime,
    run_id: str = "",
    bounds: tuple[datetime, datetime] | None = None,
) -> list[CostEvent]:
    matching_costs: list[CostEvent] = []
    for item in tenant_costs:
        if item.cost_type == "adjustment":
            continue
        if bounds is not None:
            dt = _ensure_utc(item.created_at) or now
            if dt < bounds[0] or dt > bounds[1] or dt > now:
                continue
        else:
            if not _cost_in_period(item, policy, now, run_id):
                continue
        if policy.scope == "user":
            target_user = policy.scope_id
            if target_user and item.user_id != target_user:
                continue
        elif policy.scope == "workflow":
            target_wf = policy.workflow_id or policy.scope_id
            if target_wf and item.workflow_id != target_wf:
                continue
        elif policy.scope == "project":
            target_proj = policy.project_id or policy.scope_id
            if target_proj and item.project_id != target_proj:
                continue
        elif policy.scope == "model":
            target_model = policy.model_name or policy.scope_id
            if target_model and item.model_name != target_model:
                continue
        elif policy.scope == "tool":
            if item.resource_type != "tool":
                continue
            target_tool = policy.tool_id or policy.scope_id
            if target_tool and target_tool not in _cost_tools(item):
                continue
        elif policy.scope == "mcp_server":
            target_server = policy.scope_id
            if target_server and target_server not in _cost_mcp_servers(item):
                continue
            if not target_server and not _cost_mcp_servers(item):
                continue
        if policy.provider and item.provider != policy.provider:
            continue
        if policy.model_name and item.model_name and item.model_name != policy.model_name:
            continue
        if policy.workflow_id and item.workflow_id and item.workflow_id != policy.workflow_id:
            continue
        if policy.resource_type and policy.resource_type != "all" and item.resource_type != policy.resource_type:
            continue
        if not policy.include_replay_runs and item.replay_attempt_id:
            continue
        if not policy.include_failed_runs and item.cost_type == "failed_retry":
            continue
        if not policy.include_dev_environment and (item.metadata_payload or {}).get("explicit_environment") == "dev":
            continue
        matching_costs.append(item)
    return matching_costs
