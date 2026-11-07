"""v1.5.9 labeled benchmark, honeypot probes and CI attestation trust roots.

- ``BenchmarkSample`` (``bms_``): versioned labeled samples (seed fixtures, review exports, honeypots, imports).
- ``BenchmarkRun`` (``bmr_``): reproducible detector measurement over a corpus selection.
- ``HoneypotProbe`` (``hp_``): impossible-task probes that measure an agent's propensity to cheat.
- ``TenantAttestationKey`` (``attk_``): admin-registered public keys used to verify ``ci_attested`` evidence.

All signals derived from these tables are monitor-only; they are never training rewards.
"""

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class BenchmarkSample(Base):
    __tablename__ = "benchmark_samples"
    __table_args__ = (UniqueConstraint("corpus", "evidence_digest", name="uq_benchmark_samples_corpus_digest"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sample_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    corpus: Mapped[str] = mapped_column(String(160), index=True, nullable=False)
    scope: Mapped[str] = mapped_column(String(16), index=True, nullable=False, default="tenant")
    tenant_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False, default="default")
    source: Mapped[str] = mapped_column(String(32), index=True, nullable=False, default="seed_fixture")
    split: Mapped[str] = mapped_column(String(16), index=True, nullable=False, default="dev")
    name: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    label: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    label_categories: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    label_notes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    finding_labels: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    task: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    evidence: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    evidence_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    origin_review_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False, default="")
    origin_run_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), index=True, nullable=False, default="active")
    label_history: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    created_at = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class BenchmarkRun(Base):
    __tablename__ = "benchmark_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    benchmark_run_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False, default="default")
    corpus_selector: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    corpus_digest: Mapped[str] = mapped_column(String(64), index=True, nullable=False, default="")
    evaluator_version: Mapped[str] = mapped_column(String(64), index=True, nullable=False, default="")
    detector_modes: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    positive_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    negative_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    metrics: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    per_sample: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    gate_status: Mapped[str] = mapped_column(String(16), index=True, nullable=False, default="")
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    created_at = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class HoneypotProbe(Base):
    __tablename__ = "honeypot_probes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    probe_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    task_type: Mapped[str] = mapped_column(String(32), nullable=False, default="code_fix")
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    package: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    conflict_test_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    expected_honest_signals: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    builtin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, index=True, nullable=False, default=True)
    created_at = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class TenantAttestationKey(Base):
    __tablename__ = "tenant_attestation_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    algorithm: Mapped[str] = mapped_column(String(32), nullable=False)
    public_key_pem: Mapped[str] = mapped_column(Text, nullable=False)
    key_fingerprint: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    allowed_subjects: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(16), index=True, nullable=False, default="active")
    created_by: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    created_at = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    revoked_at = mapped_column(DateTime(timezone=True), nullable=True)
