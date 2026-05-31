from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.control_plane import ExecutionPlan, ToolDefinition, WorkflowDefinition
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
        workflows: list[dict] | None = None,
        tools: list[dict] | None = None,
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
            workflow_id=workflow["workflow_id"],
            workflow_version=workflow["version"],
            status=status,
            model_route={
                "model": "deterministic-control-plane",
                "routing_reason": f"V1.2 deterministic route for {request.task_type}.",
            },
            selected_tools=selected_tools,
            steps=steps,
            approval_gates=approval_gates,
            policy_snapshot=policy_snapshot,
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
    await session.commit()
    await session.refresh(row)
    return row


async def get_execution_plan_for_run(session: AsyncSession, run_id: str) -> ExecutionPlan | None:
    return await session.scalar(select(ExecutionPlan).where(ExecutionPlan.run_id == run_id))


async def latest_execution_plans(session: AsyncSession, *, limit: int = 20) -> list[ExecutionPlan]:
    stmt = select(ExecutionPlan).order_by(ExecutionPlan.created_at.desc()).limit(limit)
    return list((await session.scalars(stmt)).all())
