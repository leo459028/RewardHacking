from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_run import AgentRun
from app.models.checkpoint import ReplayAttempt, RunCheckpoint
from app.models.control_plane import ExecutionPlan
from app.models.finops import BudgetPolicy, BudgetViolation, CostEvent, PricingCatalog, UsageEvent
from app.schemas.agent import AgentRequest
from app.schemas.finops import (
    BudgetPolicyCreateRequest,
    BudgetPolicyUpdateRequest,
    PricingCatalogCreateRequest,
    PricingCatalogUpdateRequest,
    UsageEventCreateRequest,
)


DEFAULT_PRICING: tuple[dict, ...] = (
    {
        "provider": "internal",
        "resource_type": "model",
        "model_name": "deterministic-control-plane",
        "price_unit": "input_tokens",
        "unit_price": Decimal("0.00000010"),
    },
    {
        "provider": "internal",
        "resource_type": "model",
        "model_name": "deterministic-control-plane",
        "price_unit": "output_tokens",
        "unit_price": Decimal("0.00000020"),
    },
    {
        "provider": "internal",
        "resource_type": "tool",
        "model_name": "",
        "price_unit": "tool_call",
        "unit_price": Decimal("0.00050000"),
    },
    {
        "provider": "internal",
        "resource_type": "compute",
        "model_name": "",
        "price_unit": "worker_seconds",
        "unit_price": Decimal("0.00001000"),
    },
    {
        "provider": "internal",
        "resource_type": "storage",
        "model_name": "",
        "price_unit": "artifact_mb",
        "unit_price": Decimal("0.00000100"),
    },
    {
        "provider": "internal",
        "resource_type": "replay",
        "model_name": "",
        "price_unit": "replay_attempt",
        "unit_price": Decimal("0.00020000"),
    },
    {
        "provider": "internal",
        "resource_type": "checkpoint",
        "model_name": "",
        "price_unit": "checkpoint_write",
        "unit_price": Decimal("0.00010000"),
    },
)


@dataclass(frozen=True)
class RunCostSummary:
    run_id: str
    tenant_id: str
    estimated_cost: float
    actual_cost: float
    cost_delta: float
    currency: str
    budget_status: str
    breakdown: dict[str, float]
    usage_events: list[UsageEvent]
    cost_events: list[CostEvent]


@dataclass(frozen=True)
class TenantCostSummary:
    tenant_id: str
    today_cost: float
    month_to_date_cost: float
    budget_used_percent: float
    violation_count: int
    top_runs: list[dict]
    top_models: list[dict]
    top_workflows: list[dict]


def _money(value: object) -> Decimal:
    return Decimal(str(value or "0")).quantize(Decimal("0.00000001"))


def _quantity(value: object) -> Decimal:
    return Decimal(str(value or "0")).quantize(Decimal("0.000001"))


def _float(value: object) -> float:
    return float(value or 0)


def pricing_to_dict(item: PricingCatalog) -> dict:
    return {
        "pricing_id": item.pricing_id,
        "provider": item.provider,
        "resource_type": item.resource_type,
        "model_name": item.model_name,
        "price_unit": item.price_unit,
        "unit_price": _float(item.unit_price),
        "currency": item.currency,
        "is_active": item.is_active,
        "metadata": item.metadata_payload,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def usage_to_dict(item: UsageEvent) -> dict:
    return {
        "usage_id": item.usage_id,
        "tenant_id": item.tenant_id,
        "user_id": item.user_id,
        "run_id": item.run_id,
        "trace_id": item.trace_id,
        "workflow_id": item.workflow_id,
        "step_id": item.step_id,
        "checkpoint_id": item.checkpoint_id,
        "replay_attempt_id": item.replay_attempt_id,
        "provider": item.provider,
        "model_name": item.model_name,
        "resource_type": item.resource_type,
        "usage_unit": item.usage_unit,
        "quantity": _float(item.quantity),
        "phase": item.phase,
        "metadata": item.metadata_payload,
        "created_at": item.created_at,
    }


def cost_to_dict(item: CostEvent) -> dict:
    return {
        "cost_id": item.cost_id,
        "usage_event_id": item.usage_event_id,
        "pricing_catalog_id": item.pricing_catalog_id,
        "tenant_id": item.tenant_id,
        "run_id": item.run_id,
        "trace_id": item.trace_id,
        "workflow_id": item.workflow_id,
        "resource_type": item.resource_type,
        "model_name": item.model_name,
        "estimated_cost": _float(item.estimated_cost),
        "actual_cost": _float(item.actual_cost),
        "currency": item.currency,
        "allocation_tags": item.allocation_tags,
        "calculation_method": item.calculation_method,
        "metadata": item.metadata_payload,
        "created_at": item.created_at,
    }


def budget_to_dict(item: BudgetPolicy) -> dict:
    return {
        "budget_id": item.budget_id,
        "tenant_id": item.tenant_id,
        "scope": item.scope,
        "policy_name": item.policy_name,
        "period": item.period,
        "limit_amount": _float(item.limit_amount),
        "currency": item.currency,
        "resource_type": item.resource_type,
        "actions": item.actions,
        "is_active": item.is_active,
        "metadata": item.metadata_payload,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def violation_to_dict(item: BudgetViolation) -> dict:
    return {
        "violation_id": item.violation_id,
        "budget_id": item.budget_id,
        "tenant_id": item.tenant_id,
        "user_id": item.user_id,
        "run_id": item.run_id,
        "workflow_id": item.workflow_id,
        "model_name": item.model_name,
        "violation_type": item.violation_type,
        "severity": item.severity,
        "actual_amount": _float(item.actual_amount),
        "limit_amount": _float(item.limit_amount),
        "currency": item.currency,
        "decision": item.decision,
        "details": item.details,
        "created_at": item.created_at,
    }


async def ensure_default_pricing(session: AsyncSession) -> None:
    for item in DEFAULT_PRICING:
        existing = await session.scalar(
            select(PricingCatalog).where(
                PricingCatalog.provider == item["provider"],
                PricingCatalog.resource_type == item["resource_type"],
                PricingCatalog.model_name == item["model_name"],
                PricingCatalog.price_unit == item["price_unit"],
            )
        )
        if existing is not None:
            continue
        session.add(
            PricingCatalog(
                pricing_id=f"price_{uuid4().hex}",
                provider=item["provider"],
                resource_type=item["resource_type"],
                model_name=item["model_name"],
                price_unit=item["price_unit"],
                unit_price=item["unit_price"],
                currency="USD",
                is_active=True,
                metadata_payload={"source": "v1.5_default"},
            )
        )
    await session.flush()


async def create_pricing_item(session: AsyncSession, payload: PricingCatalogCreateRequest) -> PricingCatalog:
    item = PricingCatalog(
        pricing_id=f"price_{uuid4().hex}",
        provider=payload.provider,
        resource_type=payload.resource_type,
        model_name=payload.model_name,
        price_unit=payload.price_unit,
        unit_price=_money(payload.unit_price),
        currency=payload.currency,
        is_active=True,
        metadata_payload=payload.metadata,
    )
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item


async def update_pricing_item(
    session: AsyncSession,
    pricing_id: str,
    payload: PricingCatalogUpdateRequest,
) -> PricingCatalog | None:
    item = await session.scalar(select(PricingCatalog).where(PricingCatalog.pricing_id == pricing_id))
    if item is None:
        return None
    updates = payload.model_dump(exclude_unset=True)
    for key, value in updates.items():
        if key == "metadata":
            item.metadata_payload = value or {}
        elif key == "unit_price" and value is not None:
            item.unit_price = _money(value)
        else:
            setattr(item, key, value)
    await session.commit()
    await session.refresh(item)
    return item


async def disable_pricing_item(session: AsyncSession, pricing_id: str) -> PricingCatalog | None:
    item = await session.scalar(select(PricingCatalog).where(PricingCatalog.pricing_id == pricing_id))
    if item is None:
        return None
    item.is_active = False
    await session.commit()
    await session.refresh(item)
    return item


async def list_pricing_items(session: AsyncSession, *, active_only: bool = False) -> list[PricingCatalog]:
    stmt = select(PricingCatalog).order_by(PricingCatalog.created_at.desc())
    if active_only:
        stmt = stmt.where(PricingCatalog.is_active.is_(True))
    return list((await session.scalars(stmt)).all())


async def _lookup_pricing(session: AsyncSession, usage: UsageEvent) -> PricingCatalog | None:
    await ensure_default_pricing(session)
    stmt = (
        select(PricingCatalog)
        .where(
            PricingCatalog.is_active.is_(True),
            PricingCatalog.provider == usage.provider,
            PricingCatalog.resource_type == usage.resource_type,
            PricingCatalog.price_unit == usage.usage_unit,
        )
        .order_by(PricingCatalog.model_name.desc())
    )
    candidates = list((await session.scalars(stmt)).all())
    for item in candidates:
        if item.model_name == usage.model_name:
            return item
    for item in candidates:
        if item.model_name == "":
            return item
    return None


async def record_usage_event(session: AsyncSession, payload: UsageEventCreateRequest) -> UsageEvent:
    usage = UsageEvent(
        usage_id=f"usage_{uuid4().hex}",
        tenant_id=payload.tenant_id or "default",
        user_id=payload.user_id,
        run_id=payload.run_id,
        trace_id=payload.trace_id,
        workflow_id=payload.workflow_id,
        step_id=payload.step_id,
        checkpoint_id=payload.checkpoint_id,
        replay_attempt_id=payload.replay_attempt_id,
        provider=payload.provider or "internal",
        model_name=payload.model_name,
        resource_type=payload.resource_type,
        usage_unit=payload.usage_unit,
        quantity=_quantity(payload.quantity),
        phase=payload.phase,
        metadata_payload=payload.metadata,
    )
    session.add(usage)
    await session.flush()
    await _record_cost_for_usage(session, usage)
    await session.commit()
    await session.refresh(usage)
    return usage


async def _record_cost_for_usage(session: AsyncSession, usage: UsageEvent) -> CostEvent:
    pricing = await _lookup_pricing(session, usage)
    if pricing is None:
        session.add(
            BudgetViolation(
                violation_id=f"bv_{uuid4().hex}",
                tenant_id=usage.tenant_id,
                user_id=usage.user_id,
                run_id=usage.run_id,
                workflow_id=usage.workflow_id,
                model_name=usage.model_name,
                violation_type="missing_pricing_rule",
                severity="warning",
                actual_amount=Decimal("0"),
                limit_amount=Decimal("0"),
                currency="USD",
                decision="record_violation",
                details={"resource_type": usage.resource_type, "usage_unit": usage.usage_unit},
            )
        )
        amount = Decimal("0")
        pricing_id = ""
        currency = "USD"
    else:
        amount = _money(_quantity(usage.quantity) * _money(pricing.unit_price))
        pricing_id = pricing.pricing_id
        currency = pricing.currency

    is_actual = usage.phase == "actual"
    cost = CostEvent(
        cost_id=f"cost_{uuid4().hex}",
        usage_event_id=usage.usage_id,
        pricing_catalog_id=pricing_id,
        tenant_id=usage.tenant_id,
        run_id=usage.run_id,
        trace_id=usage.trace_id,
        workflow_id=usage.workflow_id,
        resource_type=usage.resource_type,
        model_name=usage.model_name,
        estimated_cost=Decimal("0") if is_actual else amount,
        actual_cost=amount if is_actual else Decimal("0"),
        currency=currency,
        allocation_tags={"tenant_id": usage.tenant_id, "workflow_id": usage.workflow_id},
        calculation_method=usage.phase,
        metadata_payload={"usage_unit": usage.usage_unit},
    )
    session.add(cost)
    await session.flush()
    return cost


async def list_usage_events(
    session: AsyncSession,
    *,
    run_id: str | None = None,
    tenant_id: str | None = None,
) -> list[UsageEvent]:
    stmt = select(UsageEvent).order_by(UsageEvent.created_at.asc())
    if run_id:
        stmt = stmt.where(UsageEvent.run_id == run_id)
    if tenant_id:
        stmt = stmt.where(UsageEvent.tenant_id == tenant_id)
    return list((await session.scalars(stmt)).all())


async def _list_cost_events(
    session: AsyncSession,
    *,
    run_id: str | None = None,
    tenant_id: str | None = None,
    workflow_id: str | None = None,
) -> list[CostEvent]:
    stmt = select(CostEvent).order_by(CostEvent.created_at.asc())
    if run_id:
        stmt = stmt.where(CostEvent.run_id == run_id)
    if tenant_id:
        stmt = stmt.where(CostEvent.tenant_id == tenant_id)
    if workflow_id:
        stmt = stmt.where(CostEvent.workflow_id == workflow_id)
    return list((await session.scalars(stmt)).all())


def _token_estimate(text: str) -> int:
    return max(1, len(text.split()) * 4)


async def record_submit_usage(session: AsyncSession, *, run: AgentRun, plan: ExecutionPlan | None) -> None:
    request = AgentRequest.model_validate(run.request_payload)
    workflow_id = plan.workflow_id if plan is not None else request.task_type
    trace_id = (plan.trace_id if plan is not None else request.metadata.get("trace_id", "")) or ""
    tool_count = max(1, len(request.allowed_tools or ["summarize"]))
    events = [
        UsageEventCreateRequest(
            tenant_id=run.tenant_id,
            user_id=run.user_id,
            run_id=run.run_id,
            trace_id=trace_id,
            workflow_id=workflow_id,
            step_id="step_1_plan",
            provider="internal",
            model_name="deterministic-control-plane",
            resource_type="model",
            usage_unit="input_tokens",
            quantity=_token_estimate(request.prompt + " " + request.goal),
            phase="estimated",
            metadata={"source": "submit"},
        ),
        UsageEventCreateRequest(
            tenant_id=run.tenant_id,
            user_id=run.user_id,
            run_id=run.run_id,
            trace_id=trace_id,
            workflow_id=workflow_id,
            step_id="step_2_tools",
            resource_type="tool",
            usage_unit="tool_call",
            quantity=tool_count,
            phase="estimated",
            metadata={"tools": request.allowed_tools},
        ),
        UsageEventCreateRequest(
            tenant_id=run.tenant_id,
            user_id=run.user_id,
            run_id=run.run_id,
            trace_id=trace_id,
            workflow_id=workflow_id,
            step_id="step_3_replay_bundle",
            resource_type="storage",
            usage_unit="artifact_mb",
            quantity=0.01,
            phase="estimated",
            metadata={"source": "replay_bundle_placeholder"},
        ),
    ]
    await _record_events_and_budget(session, run=run, events=events)


async def record_actual_usage(session: AsyncSession, *, run: AgentRun, plan: ExecutionPlan | None) -> None:
    request = AgentRequest.model_validate(run.request_payload)
    workflow_id = plan.workflow_id if plan is not None else request.task_type
    trace_id = (plan.trace_id if plan is not None else request.metadata.get("trace_id", "")) or ""
    transcript = run.evaluation_summary.get("execution_event", {}).get("payload", {}).get("tool_transcript", [])
    output_tokens = _token_estimate(run.execution_output)
    events = [
        UsageEventCreateRequest(
            tenant_id=run.tenant_id,
            user_id=run.user_id,
            run_id=run.run_id,
            trace_id=trace_id,
            workflow_id=workflow_id,
            step_id="step_2_tools",
            resource_type="tool",
            usage_unit="tool_call",
            quantity=max(1, len(transcript)),
            phase="actual",
            metadata={"source": "worker", "completed_tools": len(transcript)},
        ),
        UsageEventCreateRequest(
            tenant_id=run.tenant_id,
            user_id=run.user_id,
            run_id=run.run_id,
            trace_id=trace_id,
            workflow_id=workflow_id,
            step_id="step_3_evaluation",
            provider="internal",
            model_name="deterministic-control-plane",
            resource_type="model",
            usage_unit="output_tokens",
            quantity=output_tokens,
            phase="actual",
            metadata={"source": "worker"},
        ),
        UsageEventCreateRequest(
            tenant_id=run.tenant_id,
            user_id=run.user_id,
            run_id=run.run_id,
            trace_id=trace_id,
            workflow_id=workflow_id,
            step_id="worker_runtime",
            resource_type="compute",
            usage_unit="worker_seconds",
            quantity=1,
            phase="actual",
            metadata={"source": "worker"},
        ),
    ]
    await _record_events_and_budget(session, run=run, events=events)


async def record_checkpoint_usage(
    session: AsyncSession,
    *,
    run: AgentRun,
    plan: ExecutionPlan | None,
    checkpoint: RunCheckpoint,
) -> None:
    workflow_id = plan.workflow_id if plan is not None else run.task_type
    trace_id = (plan.trace_id if plan is not None else checkpoint.trace_id) or ""
    await _record_events_and_budget(
        session,
        run=run,
        events=[
            UsageEventCreateRequest(
                tenant_id=run.tenant_id,
                user_id=run.user_id,
                run_id=run.run_id,
                trace_id=trace_id,
                workflow_id=workflow_id,
                checkpoint_id=checkpoint.checkpoint_id,
                resource_type="checkpoint",
                usage_unit="checkpoint_write",
                quantity=1,
                phase="resume",
                metadata={"checkpoint_type": checkpoint.checkpoint_type},
            )
        ],
    )


async def record_replay_usage(
    session: AsyncSession,
    *,
    run: AgentRun,
    plan: ExecutionPlan,
    attempt: ReplayAttempt,
) -> None:
    await _record_events_and_budget(
        session,
        run=run,
        events=[
            UsageEventCreateRequest(
                tenant_id=run.tenant_id,
                user_id=run.user_id,
                run_id=run.run_id,
                trace_id=attempt.trace_id,
                workflow_id=plan.workflow_id,
                replay_attempt_id=attempt.replay_id,
                resource_type="replay",
                usage_unit="replay_attempt",
                quantity=1,
                phase="replay",
                metadata={"mode": attempt.mode, "external_execution": False},
            )
        ],
    )


async def _record_events_and_budget(
    session: AsyncSession,
    *,
    run: AgentRun,
    events: list[UsageEventCreateRequest],
) -> None:
    for event in events:
        usage = UsageEvent(
            usage_id=f"usage_{uuid4().hex}",
            tenant_id=event.tenant_id,
            user_id=event.user_id,
            run_id=event.run_id,
            trace_id=event.trace_id,
            workflow_id=event.workflow_id,
            step_id=event.step_id,
            checkpoint_id=event.checkpoint_id,
            replay_attempt_id=event.replay_attempt_id,
            provider=event.provider,
            model_name=event.model_name,
            resource_type=event.resource_type,
            usage_unit=event.usage_unit,
            quantity=_quantity(event.quantity),
            phase=event.phase,
            metadata_payload=event.metadata,
        )
        session.add(usage)
        await session.flush()
        await _record_cost_for_usage(session, usage)
    await _check_budgets(session, run=run)
    await session.commit()


async def create_budget_policy(session: AsyncSession, payload: BudgetPolicyCreateRequest) -> BudgetPolicy:
    item = BudgetPolicy(
        budget_id=f"budget_{uuid4().hex}",
        tenant_id=payload.tenant_id or "default",
        scope=payload.scope,
        policy_name=payload.policy_name,
        period=payload.period,
        limit_amount=_money(payload.limit_amount),
        currency=payload.currency,
        resource_type=payload.resource_type,
        actions=payload.actions,
        is_active=True,
        metadata_payload=payload.metadata,
    )
    session.add(item)
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
        elif key == "limit_amount" and value is not None:
            item.limit_amount = _money(value)
        else:
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
    limit: int = 100,
) -> list[BudgetViolation]:
    stmt = select(BudgetViolation).order_by(BudgetViolation.created_at.desc()).limit(limit)
    if tenant_id:
        stmt = stmt.where(BudgetViolation.tenant_id == tenant_id)
    if run_id:
        stmt = stmt.where(BudgetViolation.run_id == run_id)
    return list((await session.scalars(stmt)).all())


async def _check_budgets(session: AsyncSession, *, run: AgentRun) -> None:
    policies = await list_budget_policies(session, tenant_id=run.tenant_id, active_only=True)
    if not policies:
        return
    summary = await run_cost_summary(session, run_id=run.run_id)
    run_total = _money(summary.estimated_cost + summary.actual_cost)
    for policy in policies:
        if policy.scope != "run":
            continue
        if run_total <= _money(policy.limit_amount):
            continue
        existing = await session.scalar(
            select(BudgetViolation).where(
                BudgetViolation.budget_id == policy.budget_id,
                BudgetViolation.run_id == run.run_id,
                BudgetViolation.violation_type == "run_budget_exceeded",
            )
        )
        if existing is not None:
            continue
        decision = "fail_closed" if "fail_closed" in policy.actions else "warn"
        session.add(
            BudgetViolation(
                violation_id=f"bv_{uuid4().hex}",
                budget_id=policy.budget_id,
                tenant_id=run.tenant_id,
                user_id=run.user_id,
                run_id=run.run_id,
                workflow_id=summary.cost_events[0].workflow_id if summary.cost_events else "",
                violation_type="run_budget_exceeded",
                severity="high" if decision == "fail_closed" else "warning",
                actual_amount=run_total,
                limit_amount=_money(policy.limit_amount),
                currency=policy.currency,
                decision=decision,
                details={"actions": policy.actions, "scope": policy.scope, "period": policy.period},
            )
        )


async def run_cost_summary(session: AsyncSession, *, run_id: str) -> RunCostSummary:
    usage = await list_usage_events(session, run_id=run_id)
    costs = await _list_cost_events(session, run_id=run_id)
    estimated = sum(_float(item.estimated_cost) for item in costs)
    actual = sum(_float(item.actual_cost) for item in costs)
    tenant_id = usage[0].tenant_id if usage else ""
    breakdown: dict[str, float] = {}
    for item in costs:
        key = f"{item.resource_type}_cost"
        breakdown[key] = round(breakdown.get(key, 0.0) + _float(item.estimated_cost) + _float(item.actual_cost), 8)
    violations = await list_budget_violations(session, run_id=run_id)
    if any(item.decision == "fail_closed" for item in violations):
        budget_status = "exceeded"
    elif violations:
        budget_status = "warning"
    else:
        budget_status = "within_budget"
    return RunCostSummary(
        run_id=run_id,
        tenant_id=tenant_id,
        estimated_cost=round(estimated, 8),
        actual_cost=round(actual, 8),
        cost_delta=round(actual - estimated, 8),
        currency=costs[0].currency if costs else "USD",
        budget_status=budget_status,
        breakdown=breakdown,
        usage_events=usage,
        cost_events=costs,
    )


async def tenant_cost_summary(session: AsyncSession, *, tenant_id: str) -> TenantCostSummary:
    costs = await _list_cost_events(session, tenant_id=tenant_id)
    total_by_run: dict[str, float] = {}
    total_by_model: dict[str, float] = {}
    total_by_workflow: dict[str, float] = {}
    today = datetime.now(timezone.utc).date()
    today_cost = 0.0
    month_cost = 0.0
    for item in costs:
        amount = _float(item.estimated_cost) + _float(item.actual_cost)
        total_by_run[item.run_id] = total_by_run.get(item.run_id, 0.0) + amount
        if item.model_name:
            total_by_model[item.model_name] = total_by_model.get(item.model_name, 0.0) + amount
        if item.workflow_id:
            total_by_workflow[item.workflow_id] = total_by_workflow.get(item.workflow_id, 0.0) + amount
        if item.created_at and item.created_at.date() == today:
            today_cost += amount
        if item.created_at and item.created_at.year == today.year and item.created_at.month == today.month:
            month_cost += amount
    violations = await list_budget_violations(session, tenant_id=tenant_id)
    budgets = await list_budget_policies(session, tenant_id=tenant_id, active_only=True)
    monthly_limit = sum(_float(item.limit_amount) for item in budgets if item.period == "monthly")
    return TenantCostSummary(
        tenant_id=tenant_id,
        today_cost=round(today_cost, 8),
        month_to_date_cost=round(month_cost, 8),
        budget_used_percent=round((month_cost / monthly_limit * 100), 2) if monthly_limit else 0.0,
        violation_count=len(violations),
        top_runs=_top_items(total_by_run, "run_id"),
        top_models=_top_items(total_by_model, "model_name"),
        top_workflows=_top_items(total_by_workflow, "workflow_id"),
    )


def _top_items(values: dict[str, float], key: str) -> list[dict]:
    return [
        {key: name, "cost": round(cost, 8)}
        for name, cost in sorted(values.items(), key=lambda item: item[1], reverse=True)[:10]
        if name
    ]


async def workflow_cost_summary(
    session: AsyncSession,
    *,
    workflow_id: str,
    tenant_id: str | None = None,
) -> dict:
    costs = await _list_cost_events(session, workflow_id=workflow_id, tenant_id=tenant_id)
    return {
        "workflow_id": workflow_id,
        "tenant_id": tenant_id or "",
        "total_cost": round(sum(_float(c.estimated_cost) + _float(c.actual_cost) for c in costs), 8),
    }


async def model_cost_ranking(session: AsyncSession, *, tenant_id: str | None = None) -> list[dict]:
    costs = await _list_cost_events(session, tenant_id=tenant_id)
    values: dict[str, float] = {}
    for item in costs:
        if item.model_name:
            values[item.model_name] = values.get(item.model_name, 0.0) + _float(item.estimated_cost) + _float(item.actual_cost)
    return _top_items(values, "model_name")
