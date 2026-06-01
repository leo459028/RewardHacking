from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.audit import RunRecord


class RunCheckpointRecord(BaseModel):
    checkpoint_id: str
    run_id: str
    tenant_id: str
    trace_id: str = ""
    checkpoint_type: str
    status: str
    resume_cursor: str = ""
    state_payload: dict[str, Any] = Field(default_factory=dict)
    created_by: str = "system"
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


class ResumeRunRequest(BaseModel):
    resume_reason: str = Field(min_length=1, max_length=500)
    resume_cursor: str = "worker_queue"
    created_by: str = "system"


class ResumeRunResponse(BaseModel):
    run: RunRecord
    checkpoint: RunCheckpointRecord


class ReplayRunRequest(BaseModel):
    mode: Literal["dry_run"] = "dry_run"
    created_by: str = "system"


class ReplayAttemptRecord(BaseModel):
    replay_id: str
    source_run_id: str
    tenant_id: str
    trace_id: str = ""
    mode: str
    status: str
    result_payload: dict[str, Any] = Field(default_factory=dict)
    limitations: dict[str, Any] = Field(default_factory=dict)
    created_by: str = "system"
    created_at: datetime | None = None

    model_config = {"from_attributes": True}
