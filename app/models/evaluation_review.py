"""v1.5.8 evidence-based evaluation persistence (extended in v1.5.9).

- ``RunOutcome`` (``outcome_``): append-only agent outcome evidence submissions.
- ``EvaluationReview`` (``review_``): post-execution review queue (separate from pre-execution approvals).
- ``TenantEvaluationPolicy``: tenant-level protected-path policy (admin controlled).
"""

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class RunOutcome(Base):
    __tablename__ = "run_outcomes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    outcome_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False, default="default")
    submission_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    provenance: Mapped[str] = mapped_column(String(32), index=True, nullable=False, default="self_reported")
    submitted_by: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    submitted_by_key_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    evidence: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    evidence_digest: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    verdict: Mapped[str] = mapped_column(String(32), index=True, nullable=False, default="")
    review_priority: Mapped[str] = mapped_column(String(8), nullable=False, default="")
    assurance_level: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    evaluator_version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    findings: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # v1.5.9 self-reported agent identity (monitor-only cohorts), honeypot outcome and CI attestation verification
    agent_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False, default="")
    agent_version: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    model_name: Mapped[str] = mapped_column(String(128), index=True, nullable=False, default="")
    honeypot_probe_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False, default="")
    honeypot_outcome: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    attestation_status: Mapped[str] = mapped_column(String(32), nullable=False, default="none")
    attestation_digest: Mapped[str] = mapped_column(String(64), index=True, nullable=False, default="")
    attestation_detail: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class EvaluationReview(Base):
    __tablename__ = "evaluation_reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    review_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False, default="default")
    status: Mapped[str] = mapped_column(String(32), index=True, nullable=False, default="open")
    priority: Mapped[str] = mapped_column(String(8), index=True, nullable=False, default="P3")
    categories: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    finding_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    findings: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    sample_reason: Mapped[str] = mapped_column(String(32), nullable=False, default="rule_review")
    evaluator_version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    submitted_by_key_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    submitted_by: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    label: Mapped[str] = mapped_column(String(32), index=True, nullable=False, default="")
    label_categories: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    reviewer: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    reviewer_key_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # v1.5.9 agent identity, per-finding labels and benchmark export link
    agent_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False, default="")
    agent_version: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    model_name: Mapped[str] = mapped_column(String(128), index=True, nullable=False, default="")
    finding_labels: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    exported_sample_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    # v1.5.10 advisory judge triage (ordering only; never changes ``priority`` or ``status``)
    triage_priority: Mapped[str] = mapped_column(String(8), nullable=False, default="")
    triage_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    triage_source: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    judge_assessment_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    opened_at = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    decided_at = mapped_column(DateTime(timezone=True), nullable=True)


class TenantEvaluationPolicy(Base):
    __tablename__ = "tenant_evaluation_policies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    protected_paths: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    allow_default_protected_paths: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # v1.5.11 tenant consent before evidence may be sent to an external (cloud) judge profile
    allow_external_judge: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    external_judge_profiles: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    updated_by: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    created_at = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
