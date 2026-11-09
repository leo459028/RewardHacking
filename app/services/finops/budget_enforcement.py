"""Budget policy evaluation and run budget enforcement."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.agent_run import AgentRun
from app.models.approval_event import ApprovalEvent
from app.models.control_plane import ExecutionPlan
from app.models.finops import (
    BudgetViolation,
)
from app.models.incident import Incident
from app.observability.metrics import (
    FINOPS_BUDGET_VIOLATIONS_TOTAL,
    INCIDENTS_TOTAL,
)
from app.services.finops.budget_policies import (
    _matching_costs_for_policy,
    list_budget_policies,
)
from app.services.finops.core import (
    FINOPS_CALCULATION_VERSION,
    BudgetEnforcementOutcome,
    _decimal_json,
    _ensure_utc,
    _money,
    format_decimal_plain,
)
from app.services.finops.forecast import (
    _compute_burn_rate_forecasts_from_data,
)
from app.services.finops.leases import (
    release_run_budget_lease,
)
from app.services.finops.ledger import (
    _list_cost_events,
)
from app.services.finops.settlement import (
    _cost_for_basis,
    _cost_mcp_servers,
    _settled_cost_events,
)


async def evaluate_budget_policies(
    session: AsyncSession,
    *,
    run: AgentRun,
    plan: ExecutionPlan | None = None,
) -> BudgetEnforcementOutcome:
    outcome = BudgetEnforcementOutcome()
    policies = await list_budget_policies(session, tenant_id=run.tenant_id, active_only=True)
    if not policies:
        return outcome

    now = datetime.now(timezone.utc)
    settings = get_settings()
    base_currency = settings.finops_base_currency
    tenant_costs = await _list_cost_events(session, tenant_id=run.tenant_id)
    run_costs = [c for c in tenant_costs if c.run_id == run.run_id and c.cost_type != "adjustment"]
    settled_run_costs = _settled_cost_events(run_costs)
    run_estimated = _money(sum(_cost_for_basis(c, "estimated") for c in settled_run_costs))
    workflow_id = (
        plan.workflow_id
        if plan is not None
        else (run_costs[0].workflow_id if run_costs else run.task_type)
    )
    allowed_tools = list((run.request_payload or {}).get("allowed_tools") or [])
    run_mcp_servers = set(
        str(item)
        for item in (
            ((plan.model_route or {}).get("mcp_route") or {}).get("mcp_servers")
            if plan is not None
            else (((run.request_payload or {}).get("metadata") or {}).get("mcp_gateway") or {}).get("mcp_servers")
        )
        or []
    )
    for item in run_costs:
        run_mcp_servers.update(_cost_mcp_servers(item))

    decision_priority = {
        "allow": 0,
        "warn": 1,
        "degrade_model": 2,
        "dry_run_only": 3,
        "require_approval": 4,
        "fail_closed": 5,
    }

    for policy in policies:
        eff_from = _ensure_utc(policy.effective_from)
        eff_to = _ensure_utc(policy.effective_to)
        if eff_from is not None and eff_from > now:
            continue
        if eff_to is not None and eff_to < now:
            continue

        if policy.scope == "run" and policy.scope_id and policy.scope_id not in {"default", "*", run.tenant_id, run.run_id}:
            continue
        if policy.scope == "tenant" and policy.scope_id and policy.scope_id != run.tenant_id:
            continue
        if policy.scope == "user":
            target_user = policy.scope_id
            if target_user and target_user != run.user_id:
                continue
        if policy.scope == "project":
            target_proj = policy.project_id or policy.scope_id
            run_proj = str(
                (plan.model_route or {}).get("project_id")
                if plan is not None
                else ((run.request_payload or {}).get("metadata") or {}).get("project_id") or ""
            )
            if not run_proj and run_costs:
                run_proj = str(run_costs[0].project_id or "")
            if target_proj and target_proj != run_proj:
                continue
        if policy.scope == "model":
            target_model = policy.model_name or policy.scope_id
            run_model = str(
                (plan.model_route or {}).get("model")
                if plan is not None
                else ((run.request_payload or {}).get("metadata") or {}).get("model_name") or "deterministic-control-plane"
            )
            if target_model and target_model != run_model:
                continue
        if policy.scope == "workflow":
            target_wf = policy.workflow_id or policy.scope_id
            if target_wf and target_wf != workflow_id:
                continue
        if policy.scope == "tool":
            target_tool = policy.tool_id or policy.scope_id
            if target_tool and target_tool not in allowed_tools:
                continue
        if policy.scope == "mcp_server":
            target_server = policy.scope_id
            if target_server and target_server not in run_mcp_servers:
                continue

        matching_costs = _matching_costs_for_policy(tenant_costs, policy, now, run_id=run.run_id)
        settled_matching = _settled_cost_events(matching_costs)
        spent_before = _money(
            sum(_cost_for_basis(c, policy.budget_basis) for c in settled_matching if c.run_id != run.run_id)
        )
        current_run_amount = _money(
            sum(_cost_for_basis(c, policy.budget_basis) for c in settled_matching if c.run_id == run.run_id)
        )
        total_amount = _money(spent_before + current_run_amount)

        policy_curr = (policy.currency or base_currency).strip().upper()
        limit_fx_rate = Decimal("1")
        if policy_curr != base_currency:
            limit_fx_rate = settings.finops_fx_rates.get(policy_curr, Decimal("1"))

        hard_limit = _money(_money(policy.hard_limit_amount) * limit_fx_rate)
        soft_limit = _money(_money(policy.soft_limit_amount) * limit_fx_rate)
        if hard_limit == Decimal("0") and soft_limit == Decimal("0") and policy.limit_amount:
            hard_limit = _money(_money(policy.limit_amount) * limit_fx_rate)

        threshold_type = ""
        limit_used = Decimal("0")
        actions: list[str] = []
        decision = "allow"
        action_taken = "record_violation"
        severity = "medium"

        if hard_limit > Decimal("0") and total_amount > hard_limit:
            threshold_type = "hard"
            limit_used = hard_limit
            actions = [str(a) for a in (policy.actions_on_hard_limit or policy.actions or ["record_violation", "warn"])]
            if policy.approval_required and "require_approval" not in actions and "fail_closed" not in actions:
                actions.append("require_approval")
            if "fail_closed" in actions:
                decision = "fail_closed"
                action_taken = "fail_closed"
                severity = "high"
            elif "require_approval" in actions or policy.approval_required:
                decision = "require_approval"
                action_taken = "require_approval"
                severity = "high"
            elif "degrade_model" in actions:
                decision = "degrade_model"
                action_taken = "degrade_model"
                severity = "medium"
            elif "dry_run_only" in actions:
                decision = "dry_run_only"
                action_taken = "dry_run_only"
                severity = "medium"
            else:
                decision = "warn"
                action_taken = "record_violation"
                severity = "medium"
        elif soft_limit > Decimal("0") and total_amount > soft_limit:
            threshold_type = "soft"
            limit_used = soft_limit
            actions = [str(a) for a in (policy.actions_on_soft_limit or policy.actions or ["record_violation", "warn"])]
            if "degrade_model" in actions or policy.fallback_model_tier:
                decision = "degrade_model"
                action_taken = "degrade_model"
                severity = "medium"
            elif "require_approval" in actions:
                decision = "require_approval"
                action_taken = "require_approval"
                severity = "medium"
            else:
                decision = "warn"
                action_taken = "warn" if "warn" in actions else "record_violation"
                severity = "low"
        else:
            continue

        violation_type = (
            "run_budget_exceeded"
            if (policy.scope == "run" and threshold_type == "hard")
            else f"{policy.scope}_budget_{threshold_type}_limit_exceeded"
        )
        existing = await session.scalar(
            select(BudgetViolation).where(
                BudgetViolation.budget_id == policy.budget_id,
                BudgetViolation.run_id == run.run_id,
                BudgetViolation.threshold_type == threshold_type,
            )
        )
        if existing is None:
            remaining_before = max(Decimal("0"), _money(limit_used - spent_before))
            overage = _money(total_amount - limit_used)
            violation = BudgetViolation(
                violation_id=f"bv_{uuid4().hex}",
                budget_id=policy.budget_id,
                budget_policy_version=policy.policy_version,
                tenant_id=run.tenant_id,
                scope=policy.scope,
                scope_id=policy.scope_id,
                project_id=policy.project_id,
                environment=policy.environment,
                user_id=run.user_id,
                run_id=run.run_id,
                workflow_id=workflow_id,
                provider=run_costs[0].provider if run_costs else "",
                model_name=run_costs[0].model_name if run_costs else "",
                resource_type=run_costs[0].resource_type if run_costs else "",
                violation_type=violation_type,
                severity=severity,
                threshold_type=threshold_type,
                estimated_amount=run_estimated,
                actual_amount=total_amount,
                limit_amount=limit_used,
                remaining_budget_before=remaining_before,
                overage_amount=overage,
                overage_percent=_money((overage / limit_used * 100) if limit_used else 0),
                currency=base_currency,
                decision=decision,
                recommended_action=decision,
                action_taken=action_taken,
                details={
                    "actions": policy.actions,
                    "actions_on_soft_limit": policy.actions_on_soft_limit,
                    "actions_on_hard_limit": policy.actions_on_hard_limit,
                    "scope": policy.scope,
                    "period": policy.period,
                    "fallback_model_tier": policy.fallback_model_tier,
                    "policy_currency": policy_curr,
                    "limit_fx_rate": _decimal_json(limit_fx_rate),
                },
            )
            session.add(violation)
            await session.flush()
            FINOPS_BUDGET_VIOLATIONS_TOTAL.labels(
                tenant_id=run.tenant_id,
                scope=policy.scope,
                threshold_type=threshold_type,
                decision=decision,
            ).inc()
            outcome.violations.append(violation)
        else:
            outcome.violations.append(existing)

        for act in actions:
            if act not in outcome.triggered_actions:
                outcome.triggered_actions.append(act)
        if decision == "degrade_model" or "degrade_model" in actions:
            outcome.fallback_model_tier = policy.fallback_model_tier or outcome.fallback_model_tier or "cheap"
            for flag in ("finops_model_degraded", "finops_budget_degraded_model"):
                if flag not in outcome.policy_flags:
                    outcome.policy_flags.append(flag)
        if decision == "dry_run_only" or "dry_run_only" in actions:
            if "finops_budget_dry_run_only" not in outcome.policy_flags:
                outcome.policy_flags.append("finops_budget_dry_run_only")
        if decision == "require_approval":
            outcome.approval_required = True
            if "finops_budget_requires_approval" not in outcome.policy_flags:
                outcome.policy_flags.append("finops_budget_requires_approval")
        if decision == "fail_closed":
            if "finops_budget_hard_limit_exceeded" not in outcome.policy_flags:
                outcome.policy_flags.append("finops_budget_hard_limit_exceeded")

        if decision_priority.get(decision, 0) > decision_priority.get(outcome.decision, 0):
            outcome.decision = decision
            outcome.matched_budget_id = policy.budget_id
            outcome.reason = (
                f"Budget policy {policy.policy_name} ({policy.scope}/{policy.period}) {threshold_type} limit exceeded: "
                f"{format_decimal_plain(total_amount)} > {format_decimal_plain(limit_used)} {base_currency}."
            )

    forecast_resp = _compute_burn_rate_forecasts_from_data(
        policies,
        tenant_costs,
        tenant_id=run.tenant_id,
        now=now,
        record_metrics=True,
    )
    if forecast_resp.burn_status in {"elevated_burn", "critical_burn"}:
        if "finops_burn_rate_warning" not in outcome.policy_flags:
            outcome.policy_flags.append("finops_burn_rate_warning")

    return outcome


async def _check_budgets(
    session: AsyncSession,
    *,
    run: AgentRun,
    plan: ExecutionPlan | None = None,
) -> BudgetEnforcementOutcome:
    return await evaluate_budget_policies(session, run=run, plan=plan)


async def apply_budget_enforcement_to_run(
    session: AsyncSession,
    *,
    run: AgentRun,
    plan: ExecutionPlan | None,
    result: object | None,
    outcome: BudgetEnforcementOutcome,
) -> None:
    if outcome.decision == "allow" and not outcome.policy_flags:
        return

    flags = list(run.policy_flags or [])
    for flag in outcome.policy_flags:
        if flag not in flags:
            flags.append(flag)
    run.policy_flags = flags
    if result is not None and hasattr(result, "response"):
        for flag in outcome.policy_flags:
            if flag not in result.response.policy_flags:
                result.response.policy_flags.append(flag)

    summary = dict(run.evaluation_summary or {})

    if "finops_model_degraded" in outcome.policy_flags and plan is not None:
        tier = outcome.fallback_model_tier or "cheap"
        old_route = dict(plan.model_route or {})
        provider_name = str(old_route.get("provider") or "internal")
        old_model = str(old_route.get("model") or "deterministic-control-plane")
        degraded_model = f"deterministic-{tier}" if provider_name == "internal" else f"{old_model}-{tier}"
        plan.model_route = {
            **old_route,
            "model": degraded_model,
            "model_tier": tier,
            "fallback_model_tier": tier,
            "degraded_from": old_model,
            "routing_reason": f"{FINOPS_CALCULATION_VERSION.upper()} FinOps budget soft-limit degraded model route to {tier} tier.",
        }
        exec_plan_dict = dict(summary.get("execution_plan") or {})
        if exec_plan_dict:
            exec_plan_dict["model_route"] = plan.model_route
            summary["execution_plan"] = exec_plan_dict

    if outcome.decision == "dry_run_only" or "finops_budget_dry_run_only" in outcome.policy_flags:
        req_payload = dict(run.request_payload or {})
        req_meta = dict(req_payload.get("metadata") or {})
        req_meta["forced_dry_run"] = True
        req_payload["metadata"] = req_meta
        run.request_payload = req_payload
        if plan is not None:
            pol_snap = dict(plan.policy_snapshot or {})
            pol_snap["forced_dry_run"] = True
            plan.policy_snapshot = pol_snap
            exec_plan_dict = dict(summary.get("execution_plan") or {})
            if exec_plan_dict:
                exec_plan_dict["policy_snapshot"] = pol_snap
                summary["execution_plan"] = exec_plan_dict

    if outcome.decision == "require_approval":
        reason = outcome.reason or "Budget policy requires human approval before execution."
        run.status = "awaiting_approval"
        run.decision_reason = reason
        run.requires_human_review = True
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=get_settings().approval_ttl_seconds)
        existing_approval = await session.scalar(
            select(ApprovalEvent).where(
                ApprovalEvent.run_id == run.run_id,
                ApprovalEvent.status == "pending",
            )
        )
        if existing_approval is None:
            existing_approval = ApprovalEvent(
                run_id=run.run_id,
                status="pending",
                reviewer="",
                reason=reason,
                expires_at=expires_at,
                expired=False,
            )
            session.add(existing_approval)
            await session.flush()
        approval_id_str = f"appr_{existing_approval.id}"
        for violation in outcome.violations:
            if violation.decision == "require_approval":
                violation.approval_id = approval_id_str
        if plan is not None:
            gates = list(plan.approval_gates or [])
            budget_gate = {
                "gate_type": "finops_budget_gate",
                "status": "pending",
                "budget_id": outcome.matched_budget_id,
                "reason": reason,
            }
            if not any(g.get("gate_type") == "finops_budget_gate" for g in gates):
                gates.append(budget_gate)
            plan.approval_gates = gates
            plan.status = "requires_approval"
            steps = list(plan.steps or [])
            if not any(s.get("step_type") == "approval_gate" for s in steps):
                steps.insert(
                    1,
                    {
                        "step_id": "approval-gate",
                        "step_type": "approval_gate",
                        "status": "pending",
                        "gates": gates,
                    },
                )
            plan.steps = steps
            exec_plan_dict = dict(summary.get("execution_plan") or {})
            if exec_plan_dict:
                exec_plan_dict["approval_gates"] = gates
                exec_plan_dict["status"] = "requires_approval"
                exec_plan_dict["steps"] = steps
                summary["execution_plan"] = exec_plan_dict
        summary["run_state"] = {"status": "awaiting_approval"}
        summary["approval"] = {
            "status": "pending",
            "reviewer": "",
            "reason": reason,
            "expires_at": expires_at.isoformat(),
            "expired": False,
        }
        if result is not None and hasattr(result, "response"):
            result.response.status = "awaiting_approval"
            result.response.decision_reason = reason

    elif outcome.decision == "fail_closed":
        reason = outcome.reason or "Budget policy hard limit exceeded (fail_closed)."
        run.status = "denied"
        run.decision_reason = reason
        run.requires_human_review = True
        session.add(
            Incident(
                run_id=run.run_id,
                severity="high",
                code="finops_budget_exceeded",
                detail=reason,
            )
        )
        INCIDENTS_TOTAL.labels(severity="high", code="finops_budget_exceeded").inc()
        if plan is not None:
            plan.status = "blocked"
            exec_plan_dict = dict(summary.get("execution_plan") or {})
            if exec_plan_dict:
                exec_plan_dict["status"] = "blocked"
                summary["execution_plan"] = exec_plan_dict
        summary["run_state"] = {"status": "denied"}
        if result is not None and hasattr(result, "response"):
            result.response.status = "denied"
            result.response.decision_reason = reason
        await release_run_budget_lease(
            session,
            None,
            run_id=run.run_id,
            tenant_id=run.tenant_id,
            reason=reason,
        )

    run.evaluation_summary = summary
    run.result = {
        "response": {
            "run_id": run.run_id,
            "status": run.status,
            "decision_reason": run.decision_reason,
            "risk_score": run.risk_score,
            "policy_flags": run.policy_flags,
        },
        "execution_output": run.execution_output,
        "evaluation_summary": summary,
    }
    if result is not None and hasattr(result, "evaluation_summary"):
        result.evaluation_summary = summary
