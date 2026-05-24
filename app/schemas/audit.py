from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class RunRecord(BaseModel):
    run_id: str
    tenant_id: str
    user_id: str
    task_type: str
    status: str
    decision_reason: str
    risk_score: float
    policy_flags: list[str] = Field(default_factory=list)
    request_payload: dict[str, Any] = Field(default_factory=dict)
    execution_output: str
    evaluation_summary: dict[str, Any] = Field(default_factory=dict)
    requires_human_review: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class IncidentRecord(BaseModel):
    run_id: str
    severity: str
    code: str
    detail: str
    created_at: datetime

    model_config = {"from_attributes": True}


class DashboardSummaryResponse(BaseModel):
    total_runs: int
    completed: int
    escalated: int
    denied: int
    queued: int
    running: int
    awaiting_approval: int
    failed: int
    average_risk_score: float
    recent_incidents: list[IncidentRecord]


class ApprovalDecisionRequest(BaseModel):
    decision: str
    reviewer: str
    reason: str


class WorkerAttemptRecord(BaseModel):
    run_id: str
    attempt_number: int
    status: str
    failure_reason: str
    next_retry_at: datetime | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class ToolExecutionRecord(BaseModel):
    run_id: str
    tool_name: str
    status: str
    detail: str
    sandbox_policy: dict[str, Any] = Field(default_factory=dict)
    artifact_refs: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime

    model_config = {"from_attributes": True}


class EvaluationRecord(BaseModel):
    run_id: str
    task_type: str
    verdict: str
    final_score: float
    rubric: dict[str, Any] = Field(default_factory=dict)
    judge_signal: dict[str, Any] = Field(default_factory=dict)
    explanation: str
    created_at: datetime

    model_config = {"from_attributes": True}


class GovernanceEventRecord(BaseModel):
    tenant_id: str
    user_id: str
    run_id: str
    event_type: str
    decision: str
    reason: str
    limit_value: int
    used: int
    remaining: int
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime

    model_config = {"from_attributes": True}


class GovernanceSummaryResponse(BaseModel):
    tenant_id: str
    total_runs: int
    awaiting_approval: int
    denied: int
    quota_limited: int
    average_risk_score: float
    recent_events: list[GovernanceEventRecord] = Field(default_factory=list)
