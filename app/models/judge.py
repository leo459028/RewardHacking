"""v1.5.10 LLM-as-judge triage and quota-capped review sampling.

- ``JudgeAssessment`` (``jdg_``): one advisory judge assessment of a review (or of a benchmark sample).
  A judge only orders review work; it never writes verdicts, priorities, review status or run status.
- ``ReviewSamplingDecision`` (``smp_``): one sampling decision per rule-passed run (threshold slice,
  uniform random audit slice, skipped with a reason, or not selected). Nothing is silently dropped.

All signals derived from these tables are monitor-only; they are never training rewards.
"""

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class JudgeAssessment(Base):
    __tablename__ = "judge_assessments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    assessment_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False, default="default")
    run_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False, default="")
    review_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False, default="")
    outcome_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    benchmark_run_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False, default="")
    sample_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    provider: Mapped[str] = mapped_column(String(32), index=True, nullable=False, default="deterministic")
    profile: Mapped[str] = mapped_column(String(48), index=True, nullable=False, default="")  # v1.5.11 judge profile name
    model: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    judge_version: Mapped[str] = mapped_column(String(200), index=True, nullable=False, default="")
    prompt_version: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="shadow")
    status: Mapped[str] = mapped_column(String(32), index=True, nullable=False, default="ok")
    suspicion: Mapped[float | None] = mapped_column(Float, nullable=True)
    triage_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    triage_priority: Mapped[str] = mapped_column(String(8), nullable=False, default="P3")
    triage_source: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    categories: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    evidence_refs: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    rationale: Mapped[str] = mapped_column(Text, nullable=False, default="")
    fallback: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    injection_suspected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    injection_markers: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    same_family_warning: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    input_digest: Mapped[str] = mapped_column(String(64), index=True, nullable=False, default="")
    input_truncated: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    usage_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class JudgeProfileConfig(Base):
    """v1.5.12 (D4): a Dashboard-managed judge profile. ``config`` holds non-secret fields only."""

    __tablename__ = "judge_profile_configs"

    name: Mapped[str] = mapped_column(String(48), primary_key=True)
    config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    updated_by: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    created_at = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class JudgeProfileState(Base):
    """v1.5.12 (D3): last probe and activation of a judge profile from any source.

    Only configuration fingerprints are stored (D5): never an API key or a full hash of one. At most one row has
    ``active=True`` (the service clears the others in the same transaction).
    """

    __tablename__ = "judge_profile_states"

    profile_name: Mapped[str] = mapped_column(String(48), primary_key=True)
    probe_status: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    probe_error: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    probe_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    probed_at = mapped_column(DateTime(timezone=True), nullable=True)
    probe_result: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, index=True, nullable=False, default=False)
    activation_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    activated_by: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    activated_at = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class ReviewSamplingDecision(Base):
    __tablename__ = "review_sampling_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    decision_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False, default="default")
    run_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    outcome_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    day: Mapped[str] = mapped_column(String(10), index=True, nullable=False)
    slice: Mapped[str] = mapped_column(String(16), index=True, nullable=False, default="none")
    decision: Mapped[str] = mapped_column(String(16), index=True, nullable=False, default="not_selected")
    skip_reason: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    sampling_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    random_draw: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    also_threshold: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    review_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
