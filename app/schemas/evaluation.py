from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

SIGNAL_USAGE = "monitor_only"


class EvaluationResult(BaseModel):
    run_id: str
    goal_alignment: float = Field(ge=0.0, le=1.0)
    shortcut_risk: float = Field(ge=0.0, le=1.0)
    hallucination_risk: float = Field(ge=0.0, le=1.0)
    policy_violation_risk: float = Field(ge=0.0, le=1.0)
    final_score: float = Field(ge=0.0, le=1.0)
    verdict: str
    explanation: str
    rubric: dict = Field(default_factory=dict)
    judge_signal: dict = Field(default_factory=dict)
    # v1.5.8 evidence-based evaluation fields
    evaluator_version: str = ""
    evaluation_source: str = "executor"
    assurance_level: str = "simulated"
    review_priority: str = ""
    findings: list[dict[str, Any]] = Field(default_factory=list)
    # v1.5.9 deterministic ranking score (measurement and ordering only; never decides a verdict)
    suspicion_score: float = Field(default=0.0, ge=0.0, le=1.0)
    signal_usage: str = SIGNAL_USAGE


# ---------------------------------------------------------------------------
# v1.5.8 Outcome evidence (agent-reported, treated as claims until verified)
# ---------------------------------------------------------------------------


class ReportedTestSummary(BaseModel):
    passed: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    skipped: int = Field(default=0, ge=0)
    errors: int = Field(default=0, ge=0)
    total: int | None = Field(default=None, ge=0)
    command: str = Field(default="", max_length=512)


class ReportedTests(BaseModel):
    format: Literal["junit_xml", "summary"] = "summary"
    content: str | None = None
    summary: ReportedTestSummary | None = None


class BaselineTestCounts(BaseModel):
    total: int = Field(ge=0)
    passed: int | None = Field(default=None, ge=0)


class LintReport(BaseModel):
    tool: str = Field(default="", max_length=64)
    errors: int = Field(default=0, ge=0)
    warnings: int = Field(default=0, ge=0)
    content: str | None = None


class ChangedFile(BaseModel):
    path: str = Field(min_length=1, max_length=512)
    before: str | None = None
    after: str | None = None


class OutcomeEvidence(BaseModel):
    final_message: str = ""
    diff: str = ""
    changed_files: list[ChangedFile] = Field(default_factory=list, max_length=200)
    test_report: ReportedTests | None = None
    tests_before: BaselineTestCounts | None = None
    lint_report: LintReport | None = None
    tool_transcript: list[dict[str, Any]] = Field(default_factory=list, max_length=500)
    provenance: Literal["self_reported", "ci_attested"] = "self_reported"
    attestation: dict[str, Any] = Field(default_factory=dict)


class OutcomeSubmitRequest(OutcomeEvidence):
    submitted_by: str = Field(default="", max_length=128)


class EvaluationPreviewRequest(OutcomeEvidence):
    task_type: Literal["code_fix", "analysis", "data_query", "workflow", "other"] = "code_fix"
    goal: str = ""
    tenant_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class OutcomeRecord(BaseModel):
    outcome_id: str
    run_id: str
    tenant_id: str
    submission_seq: int
    provenance: str
    submitted_by: str
    verdict: str
    review_priority: str
    assurance_level: str
    evaluator_version: str
    findings: list[dict[str, Any]] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)
    # v1.5.9 attestation verification and self-reported cohort identity (honeypot fields are never exposed here)
    attestation_status: str = "none"
    attestation_detail: dict[str, Any] = Field(default_factory=dict)
    agent_id: str = ""
    model_name: str = ""
    created_at: datetime

    model_config = {"from_attributes": True}


class ReviewRecord(BaseModel):
    review_id: str
    run_id: str
    tenant_id: str
    status: str
    priority: str
    categories: list[str] = Field(default_factory=list)
    finding_ids: list[str] = Field(default_factory=list)
    findings: list[dict[str, Any]] = Field(default_factory=list)
    sample_reason: str
    evaluator_version: str
    submitted_by: str = ""
    label: str = ""
    label_categories: list[str] = Field(default_factory=list)
    reviewer: str = ""
    reason: str = ""
    opened_at: datetime
    decided_at: datetime | None = None
    # v1.5.9 self-reported cohort identity, per-finding labels and benchmark export link
    agent_id: str = ""
    agent_version: str = ""
    model_name: str = ""
    finding_labels: dict[str, str] = Field(default_factory=dict)
    exported_sample_id: str = ""
    # v1.5.10 advisory judge triage (ordering only; ``priority`` is the rule priority and never changes)
    triage_priority: str = ""
    triage_score: float | None = None
    triage_source: str = ""
    judge_assessment_id: str = ""
    effective_priority: str = ""
    signal_usage: str = SIGNAL_USAGE

    model_config = {"from_attributes": True}


class OutcomeSubmitResponse(BaseModel):
    outcome_id: str
    run_id: str
    run_status: str
    submission_seq: int
    evaluation: EvaluationResult
    review: ReviewRecord | None = None
    signal_usage: str = SIGNAL_USAGE


class EvaluationPreviewResponse(BaseModel):
    evaluation: EvaluationResult
    persisted: bool = False
    signal_usage: str = SIGNAL_USAGE


class ReviewDecisionRequest(BaseModel):
    label: Literal["confirmed_hacking", "false_positive", "accepted", "needs_info"]
    reviewer: str = Field(min_length=1, max_length=128)
    reason: str = Field(default="", max_length=2000)
    label_categories: list[str] = Field(default_factory=list, max_length=20)
    # v1.5.9 per-finding labels for detector-level precision (finding IDs must belong to the review)
    finding_labels: dict[str, Literal["true_positive", "false_positive"]] = Field(default_factory=dict, max_length=200)


class EvaluationPolicyRecord(BaseModel):
    tenant_id: str
    protected_paths: list[str] = Field(default_factory=list)
    allow_default_protected_paths: bool = True
    default_protected_paths: list[str] = Field(default_factory=list)
    effective_protected_paths: list[str] = Field(default_factory=list)
    updated_by: str = ""


class EvaluationPolicyUpdateRequest(BaseModel):
    protected_paths: list[str] = Field(default_factory=list, max_length=200)
    allow_default_protected_paths: bool = True
    updated_by: str = Field(default="", max_length=128)
