from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class WorkflowDefinitionCreateRequest(BaseModel):
    workflow_id: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=32)
    task_type: Literal["code_fix", "analysis", "data_query", "workflow", "other"]
    display_name: str = Field(min_length=1, max_length=128)
    status: str = "active"
    definition: dict[str, Any] = Field(default_factory=dict)


class WorkflowDefinitionRecord(WorkflowDefinitionCreateRequest):
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class ToolDefinitionCreateRequest(BaseModel):
    tool_name: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=128)
    status: str = "active"
    task_types: list[str] = Field(default_factory=list)
    requires_approval: bool = False
    risk_level: str = "low"
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolDefinitionRecord(BaseModel):
    tool_name: str
    display_name: str
    status: str
    task_types: list[str] = Field(default_factory=list)
    requires_approval: bool
    risk_level: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class ExecutionPlanDraft(BaseModel):
    plan_id: str
    run_id: str
    tenant_id: str = "default"
    trace_id: str = ""
    workflow_id: str
    workflow_version: str
    status: str
    model_route: dict[str, Any] = Field(default_factory=dict)
    selected_tools: list[dict[str, Any]] = Field(default_factory=list)
    steps: list[dict[str, Any]] = Field(default_factory=list)
    approval_gates: list[dict[str, Any]] = Field(default_factory=list)
    policy_snapshot: dict[str, Any] = Field(default_factory=dict)
    trace_metadata: dict[str, Any] = Field(default_factory=dict)


class ExecutionPlanRecord(ExecutionPlanDraft):
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class ExecutionPlanStepRecord(BaseModel):
    step_id: str
    plan_id: str
    run_id: str
    tenant_id: str
    trace_id: str
    step_type: str
    status: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None
    input_summary: str = ""
    output_summary: str = ""
    error_summary: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None


class RunTraceResponse(BaseModel):
    trace_id: str
    trace_metadata: dict[str, Any] = Field(default_factory=dict)
    run: dict[str, Any]
    execution_plan: dict[str, Any]
    steps: list[ExecutionPlanStepRecord] = Field(default_factory=list)


class ReplayBundleResponse(BaseModel):
    schema_version: str = "control-plane-replay-bundle/v1.3"
    trace_context: dict[str, Any]
    run: dict[str, Any]
    policy_decision: dict[str, Any]
    execution_plan: dict[str, Any]
    step_audit: list[ExecutionPlanStepRecord] = Field(default_factory=list)
    tool_references: list[dict[str, Any]] = Field(default_factory=list)
    limitations: dict[str, Any] = Field(default_factory=dict)
