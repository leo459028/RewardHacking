from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

MCP_SERVER_TOOL_PREFIX = "__mcp_server__:"
MCP_SERVER_ID_PATTERN = r"^[a-zA-Z0-9._-]{2,113}$"


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

    @field_validator("tool_name")
    @classmethod
    def _reject_reserved_prefix(cls, value: str) -> str:
        if value.strip().startswith(MCP_SERVER_TOOL_PREFIX):
            raise ValueError(f"tool_name must not start with reserved prefix '{MCP_SERVER_TOOL_PREFIX}'.")
        return value


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


class ToolBreakerActionRequest(BaseModel):
    reason: str = Field(default="Manual circuit breaker update", min_length=1, max_length=512)
    ttl_seconds: int = Field(default=1800, ge=1)


class ToolBreakerRecord(BaseModel):
    tool_name: str
    status: str = "open"
    is_open: bool = True
    reason: str = ""
    opened_at: str = ""
    ttl_seconds: int = 1800
    failure_count: int = 0


class McpServerCreateRequest(BaseModel):
    mcp_server_id: str = Field(pattern=MCP_SERVER_ID_PATTERN)
    display_name: str = Field(default="", max_length=128)
    version: str = Field(default="1.0.0", min_length=1, max_length=32)
    transport: Literal["stdio", "sse", "streamable_http"] = "stdio"
    endpoint: str = Field(default="local://stdio", min_length=1, max_length=512)
    trust_tier: Literal["trusted", "verified", "untrusted", "blocked"] = "verified"
    is_external_network: bool | None = None
    status: Literal["active", "deprecated", "disabled"] = "active"
    requires_approval: bool = False
    risk_level: Literal["low", "medium", "high", "critical"] = "low"
    quota_per_minute: int = Field(default=60, ge=1, le=100000)
    allowed_tenants: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class McpServerRecord(BaseModel):
    mcp_server_id: str
    display_name: str
    version: str = "1.0.0"
    transport: str
    endpoint: str
    trust_tier: str
    is_external_network: bool
    status: str
    requires_approval: bool
    risk_level: str
    quota_per_minute: int
    allowed_tenants: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    source: str = "builtin"
    breaker_open: bool = False


class McpBreakerRecord(BaseModel):
    mcp_server_id: str
    status: str = "open"
    is_open: bool = True
    reason: str = ""
    opened_at: str = ""
    ttl_seconds: int = 1800


class ToolArgumentValidationRequest(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)
    allow_pii: bool = False


class ToolArgumentValidationResponse(BaseModel):
    tool_name: str
    mcp_server_id: str = ""
    valid: bool
    errors: list[str] = Field(default_factory=list)
    injection_flags: list[str] = Field(default_factory=list)
    sanitized_arguments: dict[str, Any] = Field(default_factory=dict)
    redacted_fields: list[str] = Field(default_factory=list)
