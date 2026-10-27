from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.control_plane import ExecutionPlan, ExecutionPlanStep, ToolDefinition, WorkflowDefinition
from app.observability.metrics import CONTROL_PLANE_STEPS_TOTAL
from app.schemas.agent import AgentRequest
from app.schemas.control_plane import ExecutionPlanDraft


BUILTIN_WORKFLOWS: list[dict] = [
    {
        "workflow_id": "analysis-basic",
        "version": "1.0.0",
        "task_type": "analysis",
        "display_name": "Analysis Basic",
        "status": "active",
        "definition": {"steps": ["policy_check", "tool_execution", "evaluation"]},
    },
    {
        "workflow_id": "code-fix-basic",
        "version": "1.0.0",
        "task_type": "code_fix",
        "display_name": "Code Fix Basic",
        "status": "active",
        "definition": {"steps": ["policy_check", "tool_execution", "evaluation"]},
    },
    {
        "workflow_id": "data-query-basic",
        "version": "1.0.0",
        "task_type": "data_query",
        "display_name": "Data Query Basic",
        "status": "active",
        "definition": {"steps": ["policy_check", "tool_execution", "evaluation"]},
    },
    {
        "workflow_id": "workflow-controlled",
        "version": "1.0.0",
        "task_type": "workflow",
        "display_name": "Controlled Workflow",
        "status": "active",
        "definition": {"steps": ["policy_check", "approval_gate", "tool_execution", "evaluation"]},
    },
    {
        "workflow_id": "other-basic",
        "version": "1.0.0",
        "task_type": "other",
        "display_name": "Other Basic",
        "status": "active",
        "definition": {"steps": ["policy_check", "tool_execution", "evaluation"]},
    },
]


BUILTIN_TOOLS: list[dict] = [
    {
        "tool_name": "read_data",
        "display_name": "Read Data",
        "status": "active",
        "task_types": ["analysis", "data_query", "workflow"],
        "requires_approval": False,
        "risk_level": "low",
        "metadata": {"source": "builtin"},
    },
    {
        "tool_name": "inspect_docs",
        "display_name": "Inspect Docs",
        "status": "active",
        "task_types": ["analysis", "other"],
        "requires_approval": False,
        "risk_level": "low",
        "metadata": {"source": "builtin"},
    },
    {
        "tool_name": "summarize",
        "display_name": "Summarize",
        "status": "active",
        "task_types": ["analysis", "data_query", "other"],
        "requires_approval": False,
        "risk_level": "low",
        "metadata": {"source": "builtin"},
    },
    {
        "tool_name": "read_repo",
        "display_name": "Read Repo",
        "status": "active",
        "task_types": ["code_fix"],
        "requires_approval": False,
        "risk_level": "low",
        "metadata": {"source": "builtin"},
    },
    {
        "tool_name": "write_patch",
        "display_name": "Write Patch",
        "status": "active",
        "task_types": ["code_fix"],
        "requires_approval": False,
        "risk_level": "medium",
        "metadata": {"source": "builtin"},
    },
    {
        "tool_name": "run_tests",
        "display_name": "Run Tests",
        "status": "active",
        "task_types": ["code_fix"],
        "requires_approval": False,
        "risk_level": "low",
        "metadata": {"source": "builtin"},
    },
    {
        "tool_name": "query_data",
        "display_name": "Query Data",
        "status": "active",
        "task_types": ["data_query"],
        "requires_approval": False,
        "risk_level": "medium",
        "metadata": {"source": "builtin"},
    },
    {
        "tool_name": "notify_operator",
        "display_name": "Notify Operator",
        "status": "active",
        "task_types": ["workflow"],
        "requires_approval": False,
        "risk_level": "low",
        "metadata": {"source": "builtin"},
    },
    {
        "tool_name": "deploy_service",
        "display_name": "Deploy Service",
        "status": "active",
        "task_types": ["workflow"],
        "requires_approval": True,
        "risk_level": "high",
        "metadata": {"source": "builtin"},
    },
]


class ControlPlanePlanner:
    def plan(
        self,
        *,
        request: AgentRequest,
        run_id: str,
        policy_snapshot: dict,
        trace_id: str = "",
        workflows: list[dict] | None = None,
        tools: list[dict] | None = None,
        budget_outcome: object | dict | None = None,
    ) -> ExecutionPlanDraft:
        workflow = self._select_workflow(request.task_type, workflows or BUILTIN_WORKFLOWS)
        requested_tools = request.allowed_tools or ["summarize"]
        tool_map = {tool["tool_name"]: tool for tool in (tools or BUILTIN_TOOLS)}
        selected_tools: list[dict] = []
        approval_gates: list[dict] = []

        for tool_name in requested_tools:
            tool = tool_map.get(tool_name)
            if tool is None or tool.get("status") != "active":
                approval_gates.append(
                    {
                        "gate_type": "tool_registry",
                        "status": "blocked",
                        "reason": f"Tool {tool_name} is not active in the control-plane registry.",
                    }
                )
                continue
            if request.task_type not in tool.get("task_types", []):
                approval_gates.append(
                    {
                        "gate_type": "tool_task_scope",
                        "status": "blocked",
                        "reason": f"Tool {tool_name} is not allowed for task type {request.task_type}.",
                    }
                )
                continue
            selected_tools.append(
                {
                    "name": tool["tool_name"],
                    "risk_level": tool.get("risk_level", "low"),
                    "requires_approval": bool(tool.get("requires_approval", False)),
                }
            )
            if tool.get("requires_approval"):
                approval_gates.append(
                    {
                        "gate_type": "tool_approval",
                        "status": "pending",
                        "reason": f"Tool {tool_name} requires approval before execution.",
                    }
                )

        meta = dict(request.metadata or {})
        routed_provider = str(meta.get("provider") or "internal").strip() or "internal"
        routed_model = str(meta.get("model_name") or "deterministic-control-plane").strip() or "deterministic-control-plane"
        routed_pricing_mode = str(meta.get("pricing_mode") or "standard").strip() or "standard"
        routed_service_tier = str(meta.get("service_tier") or "standard").strip() or "standard"
        routed_project_id = str(meta.get("project_id") or "").strip()

        model_route: dict = {
            "provider": routed_provider,
            "model": routed_model,
            "pricing_mode": routed_pricing_mode,
            "service_tier": routed_service_tier,
            "project_id": routed_project_id,
            "routing_reason": f"V1.5.4 deterministic route for {request.task_type} ({routed_provider}/{routed_model}).",
        }
        if budget_outcome is not None:
            outcome_data = budget_outcome if isinstance(budget_outcome, dict) else getattr(budget_outcome, "__dict__", {})
            fallback_tier = outcome_data.get("fallback_model_tier")
            degraded = bool(outcome_data.get("degraded") or fallback_tier)
            if degraded and fallback_tier:
                degraded_model = (
                    f"deterministic-{fallback_tier}"
                    if routed_provider == "internal"
                    else f"{routed_model}-{fallback_tier}"
                )
                model_route = {
                    **model_route,
                    "model": degraded_model,
                    "model_tier": str(fallback_tier),
                    "fallback_model_tier": str(fallback_tier),
                    "degraded_from": routed_model,
                    "routing_reason": f"V1.5.4 FinOps budget soft-limit degraded model route to {fallback_tier} tier.",
                }
            if outcome_data.get("require_approval"):
                approval_gates.append(
                    {
                        "gate_type": "finops_budget_gate",
                        "status": "pending",
                        "budget_id": outcome_data.get("budget_id", ""),
                        "reason": outcome_data.get("reason") or "FinOps budget policy requires human approval.",
                    }
                )

        status = "planned" if not approval_gates else "requires_approval"
        steps = [
            {
                "step_id": "policy-check",
                "step_type": "policy_check",
                "status": "completed",
            },
            {
                "step_id": "tool-execution",
                "step_type": "tool_execution",
                "status": "planned" if selected_tools else "blocked",
                "tools": [tool["name"] for tool in selected_tools],
            },
            {
                "step_id": "evaluation",
                "step_type": "evaluation",
                "status": "planned",
            },
        ]
        if approval_gates:
            steps.insert(
                1,
                {
                    "step_id": "approval-gate",
                    "step_type": "approval_gate",
                    "status": "pending",
                    "gates": approval_gates,
                },
            )

        return ExecutionPlanDraft(
            plan_id=f"plan_{uuid4().hex}",
            run_id=run_id,
            tenant_id=request.tenant_id or "default",
            trace_id=trace_id,
            workflow_id=workflow["workflow_id"],
            workflow_version=workflow["version"],
            status=status,
            model_route=model_route,
            selected_tools=selected_tools,
            steps=steps,
            approval_gates=approval_gates,
            policy_snapshot=policy_snapshot,
            trace_metadata={
                "exporter_type": "internal",
                "external_trace_id": None,
                "span_id": f"span_{uuid4().hex}",
                "parent_span_id": None,
                "future_exporters": ["otlp", "langsmith-style"],
            },
        )

    def _select_workflow(self, task_type: str, workflows: list[dict]) -> dict:
        active = [
            workflow
            for workflow in workflows
            if workflow.get("task_type") == task_type and workflow.get("status", "active") == "active"
        ]
        if active:
            return sorted(active, key=lambda item: item.get("version", ""), reverse=True)[0]
        return {
            "workflow_id": "other-basic",
            "version": "1.0.0",
            "task_type": "other",
            "display_name": "Other Basic",
            "status": "active",
            "definition": {"steps": ["policy_check", "tool_execution", "evaluation"]},
        }


def workflow_to_dict(workflow: WorkflowDefinition) -> dict:
    return {
        "workflow_id": workflow.workflow_id,
        "version": workflow.version,
        "task_type": workflow.task_type,
        "display_name": workflow.display_name,
        "status": workflow.status,
        "definition": workflow.definition,
    }


def tool_to_dict(tool: ToolDefinition) -> dict:
    return {
        "tool_name": tool.tool_name,
        "display_name": tool.display_name,
        "status": tool.status,
        "task_types": tool.task_types,
        "requires_approval": tool.requires_approval,
        "risk_level": tool.risk_level,
        "metadata": tool.metadata_payload,
    }


async def list_workflow_definitions(session: AsyncSession) -> list[dict]:
    rows = list((await session.scalars(select(WorkflowDefinition))).all())
    custom = [workflow_to_dict(row) for row in rows]
    keys = {(item["workflow_id"], item["version"]) for item in custom}
    return custom + [
        item for item in BUILTIN_WORKFLOWS if (item["workflow_id"], item["version"]) not in keys
    ]


async def create_workflow_definition(
    session: AsyncSession,
    *,
    workflow_id: str,
    version: str,
    task_type: str,
    display_name: str,
    status: str,
    definition: dict,
) -> WorkflowDefinition:
    existing = await session.scalar(
        select(WorkflowDefinition).where(
            WorkflowDefinition.workflow_id == workflow_id,
            WorkflowDefinition.version == version,
        )
    )
    if existing is not None:
        existing.task_type = task_type
        existing.display_name = display_name
        existing.status = status
        existing.definition = definition
        await session.commit()
        await session.refresh(existing)
        return existing
    workflow = WorkflowDefinition(
        workflow_id=workflow_id,
        version=version,
        task_type=task_type,
        display_name=display_name,
        status=status,
        definition=definition,
    )
    session.add(workflow)
    await session.commit()
    await session.refresh(workflow)
    return workflow


async def list_tool_definitions(session: AsyncSession) -> list[dict]:
    rows = list((await session.scalars(select(ToolDefinition))).all())
    custom = [tool_to_dict(row) for row in rows]
    names = {item["tool_name"] for item in custom}
    return custom + [item for item in BUILTIN_TOOLS if item["tool_name"] not in names]


async def create_tool_definition(
    session: AsyncSession,
    *,
    tool_name: str,
    display_name: str,
    status: str,
    task_types: list[str],
    requires_approval: bool,
    risk_level: str,
    metadata: dict,
) -> ToolDefinition:
    existing = await session.scalar(select(ToolDefinition).where(ToolDefinition.tool_name == tool_name))
    if existing is not None:
        existing.display_name = display_name
        existing.status = status
        existing.task_types = task_types
        existing.requires_approval = requires_approval
        existing.risk_level = risk_level
        existing.metadata_payload = metadata
        await session.commit()
        await session.refresh(existing)
        return existing
    tool = ToolDefinition(
        tool_name=tool_name,
        display_name=display_name,
        status=status,
        task_types=task_types,
        requires_approval=requires_approval,
        risk_level=risk_level,
        metadata_payload=metadata,
    )
    session.add(tool)
    await session.commit()
    await session.refresh(tool)
    return tool


async def persist_execution_plan(
    session: AsyncSession,
    plan: ExecutionPlanDraft,
) -> ExecutionPlan:
    existing = await get_execution_plan_for_run(session, plan.run_id)
    if existing is not None:
        return existing
    row = ExecutionPlan(**plan.model_dump())
    session.add(row)
    _add_step_audit_rows(session, plan)
    await session.commit()
    await session.refresh(row)
    return row


async def get_execution_plan_for_run(session: AsyncSession, run_id: str) -> ExecutionPlan | None:
    return await session.scalar(select(ExecutionPlan).where(ExecutionPlan.run_id == run_id))


async def list_execution_plan_steps(session: AsyncSession, run_id: str) -> list[ExecutionPlanStep]:
    stmt = (
        select(ExecutionPlanStep)
        .where(ExecutionPlanStep.run_id == run_id)
        .order_by(ExecutionPlanStep.id.asc())
    )
    return list((await session.scalars(stmt)).all())


async def latest_execution_plans(session: AsyncSession, *, limit: int = 20) -> list[ExecutionPlan]:
    stmt = select(ExecutionPlan).order_by(ExecutionPlan.created_at.desc()).limit(limit)
    return list((await session.scalars(stmt)).all())


async def execution_plan_step_summaries(session: AsyncSession, run_ids: list[str]) -> dict[str, dict]:
    if not run_ids:
        return {}
    stmt = select(ExecutionPlanStep).where(ExecutionPlanStep.run_id.in_(run_ids))
    rows = list((await session.scalars(stmt)).all())
    summaries: dict[str, dict] = {}
    for row in rows:
        summary = summaries.setdefault(
            row.run_id,
            {"total": 0, "completed": 0, "blocked": 0, "failed": 0, "pending": 0, "status_counts": {}},
        )
        summary["total"] += 1
        summary["status_counts"][row.status] = summary["status_counts"].get(row.status, 0) + 1
        if row.status in summary:
            summary[row.status] += 1
        if row.status in {"blocked", "failed"}:
            summary["health"] = "attention"
        elif summary.get("health") != "attention":
            if summary["completed"] == summary["total"] and summary["total"] > 0:
                summary["health"] = "healthy"
            else:
                summary["health"] = "ready"
    return summaries


async def sync_execution_plan_on_approval(
    session: AsyncSession,
    *,
    run: object,
    decision: str,
    reviewer: str,
    reason: str,
) -> None:
    run_id = getattr(run, "run_id", "")
    plan = await get_execution_plan_for_run(session, run_id)
    if plan is None:
        return
    steps = await list_execution_plan_steps(session, run_id)
    now = datetime.now(timezone.utc)
    step_status = "completed" if decision == "approve_once" else ("blocked" if decision == "deny" else "pending")

    for row in steps:
        if row.step_type == "approval_gate" or row.step_id == "approval-gate":
            row.status = step_status
            if step_status in {"completed", "blocked"}:
                started = row.started_at
                if started is not None and started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
                row.finished_at = now
                row.duration_ms = max(0, int((now - started).total_seconds() * 1000)) if started else 0
            row.output_summary = f"Approval decision: {decision} by {reviewer} ({reason})."
            row.error_summary = reason if decision == "deny" else ""
            CONTROL_PLANE_STEPS_TOTAL.labels(step_type=row.step_type, status=row.status).inc()
        elif decision == "deny" and row.status in {"planned", "pending"}:
            row.status = "blocked"
            row.finished_at = now
            row.duration_ms = 0
            row.error_summary = f"Blocked because approval was denied by {reviewer}."

    updated_gates = []
    for gate in list(plan.approval_gates or []):
        g = dict(gate)
        g["status"] = "approved" if decision == "approve_once" else ("denied" if decision == "deny" else "pending")
        g["reviewer"] = reviewer
        g["decision_reason"] = reason
        updated_gates.append(g)
    plan.approval_gates = updated_gates

    updated_steps_json = []
    for s in list(plan.steps or []):
        sd = dict(s)
        if sd.get("step_type") == "approval_gate" or sd.get("step_id") == "approval-gate":
            sd["status"] = step_status
            sd["gates"] = updated_gates
        elif decision == "deny" and sd.get("status") in {"planned", "pending"}:
            sd["status"] = "blocked"
        updated_steps_json.append(sd)
    plan.steps = updated_steps_json
    plan.status = "planned" if decision == "approve_once" else ("blocked" if decision == "deny" else "requires_approval")

    summary = dict(getattr(run, "evaluation_summary", None) or {})
    exec_plan_dict = dict(summary.get("execution_plan") or {})
    if exec_plan_dict:
        exec_plan_dict["status"] = plan.status
        exec_plan_dict["approval_gates"] = plan.approval_gates
        exec_plan_dict["steps"] = plan.steps
        summary["execution_plan"] = exec_plan_dict
        run.evaluation_summary = summary


async def sync_execution_plan_on_worker_completion(
    session: AsyncSession,
    *,
    run: object,
    duration_ms: int = 0,
    failure_reason: str = "",
) -> None:
    run_id = getattr(run, "run_id", "")
    plan = await get_execution_plan_for_run(session, run_id)
    if plan is None:
        return
    steps = await list_execution_plan_steps(session, run_id)
    now = datetime.now(timezone.utc)
    summary = dict(getattr(run, "evaluation_summary", None) or {})
    transcript = summary.get("execution_event", {}).get("payload", {}).get("tool_transcript", [])
    eval_payload = summary.get("evaluation_event", {}).get("payload", {})
    verdict = eval_payload.get("verdict", "pass")
    final_score = eval_payload.get("final_score", 1.0)
    exec_output = str(getattr(run, "execution_output", "") or "")
    run_status = str(getattr(run, "status", "completed") or "completed")

    for row in steps:
        if row.step_type == "approval_gate" and row.status == "pending" and not failure_reason:
            row.status = "completed"
            row.finished_at = now
            row.duration_ms = 0
            row.output_summary = "Approval gate satisfied before worker execution."
        elif row.step_type == "tool_execution":
            if failure_reason:
                row.status = "failed"
                row.finished_at = now
                row.duration_ms = max(0, duration_ms)
                row.output_summary = "Tool execution failed in worker."
                row.error_summary = failure_reason[:256]
            else:
                row.status = "completed"
                row.finished_at = now
                row.duration_ms = max(1, duration_ms)
                row.output_summary = f"Completed {len(transcript)} tool call(s). {exec_output[:140]}".strip()
                row.error_summary = ""
            CONTROL_PLANE_STEPS_TOTAL.labels(step_type=row.step_type, status=row.status).inc()
        elif row.step_type == "evaluation":
            if failure_reason:
                row.status = "blocked"
                row.finished_at = now
                row.duration_ms = 0
                row.output_summary = "Evaluation skipped due to worker tool execution failure."
                row.error_summary = failure_reason[:256]
            else:
                row.status = "completed"
                row.finished_at = now
                row.duration_ms = max(1, duration_ms // 2)
                row.output_summary = f"Evaluator verdict: {verdict} (final_score={final_score})."
                row.error_summary = ""
            CONTROL_PLANE_STEPS_TOTAL.labels(step_type=row.step_type, status=row.status).inc()

    step_status_by_type = {row.step_type: row.status for row in steps}
    updated_steps_json = []
    for s in list(plan.steps or []):
        sd = dict(s)
        st = sd.get("step_type")
        if st in step_status_by_type:
            sd["status"] = step_status_by_type[st]
        updated_steps_json.append(sd)
    plan.steps = updated_steps_json

    if failure_reason:
        plan.status = "failed" if run_status == "failed" else "planned"
    else:
        plan.status = "completed" if run_status == "completed" else plan.status

    exec_plan_dict = dict(summary.get("execution_plan") or {})
    if exec_plan_dict:
        exec_plan_dict["status"] = plan.status
        exec_plan_dict["steps"] = plan.steps
        summary["execution_plan"] = exec_plan_dict
        run.evaluation_summary = summary


def step_to_dict(step: ExecutionPlanStep) -> dict:
    return {
        "step_id": step.step_id,
        "plan_id": step.plan_id,
        "run_id": step.run_id,
        "tenant_id": step.tenant_id,
        "trace_id": step.trace_id,
        "step_type": step.step_type,
        "status": step.status,
        "started_at": step.started_at,
        "finished_at": step.finished_at,
        "duration_ms": step.duration_ms,
        "input_summary": step.input_summary,
        "output_summary": step.output_summary,
        "error_summary": step.error_summary,
        "metadata": step.metadata_payload,
        "created_at": step.created_at,
    }


def _add_step_audit_rows(session: AsyncSession, plan: ExecutionPlanDraft) -> None:
    started_at = datetime.now(timezone.utc)
    parent_span_id = plan.trace_metadata.get("span_id")
    for index, step in enumerate(plan.steps):
        status = str(step.get("status", "planned"))
        row = ExecutionPlanStep(
            step_id=str(step.get("step_id") or f"step-{index + 1}"),
            plan_id=plan.plan_id,
            run_id=plan.run_id,
            tenant_id=plan.tenant_id,
            trace_id=plan.trace_id,
            step_type=str(step.get("step_type", "unknown")),
            status=status,
            started_at=started_at,
            finished_at=started_at if status in {"completed", "blocked", "failed"} else None,
            duration_ms=0 if status in {"completed", "blocked", "failed"} else None,
            input_summary=_step_input_summary(step, plan),
            output_summary=_step_output_summary(step, plan),
            error_summary=_step_error_summary(step),
            metadata_payload={
                "exporter_type": "internal",
                "external_trace_id": None,
                "span_id": f"span_{uuid4().hex}",
                "parent_span_id": parent_span_id,
                "source_plan_step_id": step.get("step_id"),
                "step_index": index,
            },
        )
        session.add(row)
        CONTROL_PLANE_STEPS_TOTAL.labels(step_type=row.step_type, status=row.status).inc()


def _step_input_summary(step: dict, plan: ExecutionPlanDraft) -> str:
    step_type = step.get("step_type")
    if step_type == "policy_check":
        return f"Policy snapshot for tenant {plan.tenant_id}."
    if step_type == "tool_execution":
        tools = ", ".join(tool["name"] for tool in plan.selected_tools) or "no active tools"
        return f"Selected tools: {tools}."
    if step_type == "approval_gate":
        return f"Approval gates: {len(plan.approval_gates)}."
    if step_type == "evaluation":
        return "Evaluation will inspect execution output and policy context."
    return f"Execution plan step {step.get('step_id', 'unknown')}."


def _step_output_summary(step: dict, plan: ExecutionPlanDraft) -> str:
    step_type = step.get("step_type")
    status = step.get("status", "planned")
    if step_type == "policy_check":
        decision = plan.policy_snapshot.get("decision", "unknown")
        return f"Policy decision: {decision}; status: {status}."
    if step_type == "tool_execution":
        return f"Tool execution status: {status}."
    if step_type == "approval_gate":
        return f"Approval gate status: {status}."
    if step_type == "evaluation":
        return f"Evaluation status: {status}."
    return f"Step status: {status}."


def _step_error_summary(step: dict) -> str:
    if step.get("status") not in {"blocked", "failed"}:
        return ""
    gates = step.get("gates") or []
    if gates:
        return "; ".join(str(gate.get("reason", "")) for gate in gates if gate.get("reason"))
    return str(step.get("reason", "Step did not complete."))
