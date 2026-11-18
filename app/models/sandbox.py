"""v1.6.2 sandbox runtime and platform re-run verification tables (plan D21, D22, D28, D42).

- ``SandboxJob`` (``sbj_``): the database work queue read by ``sandbox-runner``, the only component with Docker access.
- ``SandboxConformanceRun`` (``sbc_``): one complete run of the conformance suite; the latest row decides the gate.
- ``VerificationSuite`` (``vsu_``), ``SandboxArtifact`` and ``VerificationRun`` (``vrf_``): the platform's own copy of
  the code and tests, and the before / after results. Their services arrive in P3 and P4.
"""

from sqlalchemy import JSON, Boolean, DateTime, Index, Integer, LargeBinary, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class SandboxJob(Base):
    __tablename__ = "sandbox_jobs"
    __table_args__ = (Index("ix_sandbox_jobs_claim", "status", "kind", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # conformance | verification
    tenant_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False, default="")
    run_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    ref_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    # queued | running | completed | failed | cancelled | expired
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)  # never holds secrets
    result: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    error_code: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    claimed_by: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    claimed_at = mapped_column(DateTime(timezone=True), nullable=True)
    lease_expires_at = mapped_column(DateTime(timezone=True), nullable=True)
    created_at = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    finished_at = mapped_column(DateTime(timezone=True), nullable=True)


class SandboxConformanceRun(Base):
    __tablename__ = "sandbox_conformance_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    backend_id: Mapped[str] = mapped_column(String(32), nullable=False)
    backend_version: Mapped[str] = mapped_column(String(32), nullable=False)
    runtime: Mapped[str] = mapped_column(String(16), nullable=False)
    config_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cases: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    docker_server_version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    triggered_by: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    created_at = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True, nullable=False)


class VerificationSuite(Base):
    __tablename__ = "verification_suites"
    __table_args__ = (Index("ux_verification_suites_name_version", "tenant_id", "name", "version", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    suite_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    artifact_digest: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    base_ref: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    framework: Mapped[str] = mapped_column(String(32), nullable=False, default="pytest")
    image_name: Mapped[str] = mapped_column(String(64), nullable=False, default="python")
    test_command: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    test_paths: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")  # active | disabled
    created_by: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    created_at = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class SandboxArtifact(Base):
    __tablename__ = "sandbox_artifacts"

    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class VerificationRun(Base):
    __tablename__ = "verification_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    verification_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    outcome_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False, default="")
    tenant_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    suite_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    artifact_digest: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    trigger: Mapped[str] = mapped_column(String(16), nullable=False, default="sampled")  # sampled | all | manual | replay
    # queued | running | completed | skipped | failed
    status: Mapped[str] = mapped_column(String(16), index=True, nullable=False, default="queued")
    skip_reason: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    before: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    after: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    classification: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    coverage: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    discarded_test_changes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    backend_id: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    backend_version: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    isolation_level: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    runtime: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    config_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    findings: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    assurance_level: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    evaluator_version: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    requested_by: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    finished_at = mapped_column(DateTime(timezone=True), nullable=True)
