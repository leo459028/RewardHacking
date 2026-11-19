"""add v1.6.2 sandbox runtime and verification: job queue, conformance runs, suites, artifacts, verification runs

Revision ID: 202611180001
Revises: 202611120003
Create Date: 2026-11-18 14:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "202611180001"
down_revision = "202611120003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # D21: database work queue for sandbox-runner (SELECT ... FOR UPDATE SKIP LOCKED on Postgres).
    op.create_table(
        "sandbox_jobs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), server_default="", nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=True),
        sa.Column("ref_id", sa.String(length=64), server_default="", nullable=False),
        sa.Column("status", sa.String(length=16), server_default="queued", nullable=False),
        sa.Column("payload", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("result", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("error_code", sa.String(length=64), server_default="", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("claimed_by", sa.String(length=128), server_default="", nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_sandbox_jobs_job_id"), "sandbox_jobs", ["job_id"], unique=True)
    op.create_index(op.f("ix_sandbox_jobs_tenant_id"), "sandbox_jobs", ["tenant_id"])
    op.create_index(op.f("ix_sandbox_jobs_run_id"), "sandbox_jobs", ["run_id"])
    op.create_index("ix_sandbox_jobs_claim", "sandbox_jobs", ["status", "kind", "created_at"])

    # D14 / D42: the latest row, with a matching configuration fingerprint, opens the execution gate.
    op.create_table(
        "sandbox_conformance_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("backend_id", sa.String(length=32), nullable=False),
        sa.Column("backend_version", sa.String(length=32), nullable=False),
        sa.Column("runtime", sa.String(length=16), nullable=False),
        sa.Column("config_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("passed", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("cases", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("docker_server_version", sa.String(length=64), server_default="", nullable=False),
        sa.Column("triggered_by", sa.String(length=128), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_sandbox_conformance_runs_run_id"), "sandbox_conformance_runs", ["run_id"], unique=True)
    op.create_index(op.f("ix_sandbox_conformance_runs_created_at"), "sandbox_conformance_runs", ["created_at"])

    # D22: immutable suites; a change is a new version.
    op.create_table(
        "verification_suites",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("suite_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("artifact_digest", sa.String(length=64), nullable=False),
        sa.Column("base_ref", sa.String(length=255), server_default="", nullable=False),
        sa.Column("framework", sa.String(length=32), server_default="pytest", nullable=False),
        sa.Column("image_name", sa.String(length=64), server_default="python", nullable=False),
        sa.Column("test_command", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("test_paths", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("created_by", sa.String(length=128), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_verification_suites_suite_id"), "verification_suites", ["suite_id"], unique=True)
    op.create_index(op.f("ix_verification_suites_tenant_id"), "verification_suites", ["tenant_id"])
    op.create_index(op.f("ix_verification_suites_artifact_digest"), "verification_suites", ["artifact_digest"])
    op.create_index("ux_verification_suites_name_version", "verification_suites", ["tenant_id", "name", "version"], unique=True)

    # D22: normalized snapshots, stored once per content hash.
    op.create_table(
        "sandbox_artifacts",
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("bytes", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("sha256"),
    )

    # D28: before / after results of one platform re-run.
    op.create_table(
        "verification_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("verification_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("outcome_id", sa.String(length=64), server_default="", nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("suite_id", sa.String(length=64), nullable=False),
        sa.Column("artifact_digest", sa.String(length=64), server_default="", nullable=False),
        sa.Column("trigger", sa.String(length=16), server_default="sampled", nullable=False),
        sa.Column("status", sa.String(length=16), server_default="queued", nullable=False),
        sa.Column("skip_reason", sa.String(length=64), server_default="", nullable=False),
        sa.Column("before", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("after", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("classification", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("coverage", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("discarded_test_changes", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("backend_id", sa.String(length=32), server_default="", nullable=False),
        sa.Column("backend_version", sa.String(length=32), server_default="", nullable=False),
        sa.Column("isolation_level", sa.String(length=16), server_default="", nullable=False),
        sa.Column("runtime", sa.String(length=16), server_default="", nullable=False),
        sa.Column("config_fingerprint", sa.String(length=64), server_default="", nullable=False),
        sa.Column("findings", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("assurance_level", sa.String(length=32), server_default="", nullable=False),
        sa.Column("evaluator_version", sa.String(length=32), server_default="", nullable=False),
        sa.Column("requested_by", sa.Text(), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_verification_runs_verification_id"), "verification_runs", ["verification_id"], unique=True)
    op.create_index(op.f("ix_verification_runs_run_id"), "verification_runs", ["run_id"])
    op.create_index(op.f("ix_verification_runs_outcome_id"), "verification_runs", ["outcome_id"])
    op.create_index(op.f("ix_verification_runs_tenant_id"), "verification_runs", ["tenant_id"])
    op.create_index(op.f("ix_verification_runs_suite_id"), "verification_runs", ["suite_id"])
    op.create_index(op.f("ix_verification_runs_status"), "verification_runs", ["status"])


def downgrade() -> None:
    op.drop_index(op.f("ix_verification_runs_status"), table_name="verification_runs")
    op.drop_index(op.f("ix_verification_runs_suite_id"), table_name="verification_runs")
    op.drop_index(op.f("ix_verification_runs_tenant_id"), table_name="verification_runs")
    op.drop_index(op.f("ix_verification_runs_outcome_id"), table_name="verification_runs")
    op.drop_index(op.f("ix_verification_runs_run_id"), table_name="verification_runs")
    op.drop_index(op.f("ix_verification_runs_verification_id"), table_name="verification_runs")
    op.drop_table("verification_runs")
    op.drop_table("sandbox_artifacts")
    op.drop_index("ux_verification_suites_name_version", table_name="verification_suites")
    op.drop_index(op.f("ix_verification_suites_artifact_digest"), table_name="verification_suites")
    op.drop_index(op.f("ix_verification_suites_tenant_id"), table_name="verification_suites")
    op.drop_index(op.f("ix_verification_suites_suite_id"), table_name="verification_suites")
    op.drop_table("verification_suites")
    op.drop_index(op.f("ix_sandbox_conformance_runs_created_at"), table_name="sandbox_conformance_runs")
    op.drop_index(op.f("ix_sandbox_conformance_runs_run_id"), table_name="sandbox_conformance_runs")
    op.drop_table("sandbox_conformance_runs")
    op.drop_index("ix_sandbox_jobs_claim", table_name="sandbox_jobs")
    op.drop_index(op.f("ix_sandbox_jobs_run_id"), table_name="sandbox_jobs")
    op.drop_index(op.f("ix_sandbox_jobs_tenant_id"), table_name="sandbox_jobs")
    op.drop_index(op.f("ix_sandbox_jobs_job_id"), table_name="sandbox_jobs")
    op.drop_table("sandbox_jobs")
