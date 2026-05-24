from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class RunRecord(BaseModel):
    run_id: str
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
