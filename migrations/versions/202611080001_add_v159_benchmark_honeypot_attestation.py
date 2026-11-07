"""add v1.5.9 labeled benchmark, honeypot probes, attestation keys and cohort columns

Revision ID: 202611080001
Revises: 202611070001
Create Date: 2026-11-08 09:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "202611080001"
down_revision = "202611070001"
branch_labels = None
depends_on = None

_RUN_OUTCOME_COLUMNS = (
    ("agent_id", sa.String(length=128), "", True),
    ("agent_version", sa.String(length=128), "", False),
    ("model_name", sa.String(length=128), "", True),
    ("honeypot_probe_id", sa.String(length=64), "", True),
    ("honeypot_outcome", sa.String(length=32), "", False),
    ("attestation_status", sa.String(length=32), "none", False),
    ("attestation_digest", sa.String(length=64), "", True),
)

_REVIEW_COLUMNS = (
    ("agent_id", sa.String(length=128), "", True),
    ("agent_version", sa.String(length=128), "", False),
    ("model_name", sa.String(length=128), "", True),
    ("exported_sample_id", sa.String(length=64), "", False),
)


def upgrade() -> None:
    # Cohort, honeypot and attestation columns. Server defaults keep existing v1.5.8 rows valid without backfill.
    for name, column_type, default, indexed in _RUN_OUTCOME_COLUMNS:
        op.add_column("run_outcomes", sa.Column(name, column_type, server_default=default, nullable=False))
        if indexed:
            op.create_index(op.f(f"ix_run_outcomes_{name}"), "run_outcomes", [name])
    op.add_column("run_outcomes", sa.Column("attestation_detail", sa.JSON(), server_default=sa.text("'{}'"), nullable=False))

    for name, column_type, default, indexed in _REVIEW_COLUMNS:
        op.add_column("evaluation_reviews", sa.Column(name, column_type, server_default=default, nullable=False))
        if indexed:
            op.create_index(op.f(f"ix_evaluation_reviews_{name}"), "evaluation_reviews", [name])
    op.add_column("evaluation_reviews", sa.Column("finding_labels", sa.JSON(), server_default=sa.text("'{}'"), nullable=False))

    op.add_column("evaluation_results", sa.Column("suspicion_score", sa.Float(), server_default="0", nullable=False))

    op.create_table(
        "benchmark_samples",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("sample_id", sa.String(length=64), nullable=False),
        sa.Column("corpus", sa.String(length=160), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("split", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("label", sa.String(length=16), nullable=False),
        sa.Column("label_categories", sa.JSON(), nullable=False),
        sa.Column("label_notes", sa.JSON(), nullable=False),
        sa.Column("finding_labels", sa.JSON(), nullable=False),
        sa.Column("task", sa.JSON(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("evidence_digest", sa.String(length=64), nullable=False),
        sa.Column("origin_review_id", sa.String(length=64), nullable=False),
        sa.Column("origin_run_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("label_history", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("corpus", "evidence_digest", name="uq_benchmark_samples_corpus_digest"),
    )
    op.create_index(op.f("ix_benchmark_samples_sample_id"), "benchmark_samples", ["sample_id"], unique=True)
    for name in ("corpus", "scope", "tenant_id", "source", "split", "label", "origin_review_id", "status"):
        op.create_index(op.f(f"ix_benchmark_samples_{name}"), "benchmark_samples", [name])

    op.create_table(
        "benchmark_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("benchmark_run_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("corpus_selector", sa.JSON(), nullable=False),
        sa.Column("corpus_digest", sa.String(length=64), nullable=False),
        sa.Column("evaluator_version", sa.String(length=64), nullable=False),
        sa.Column("detector_modes", sa.JSON(), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("positive_count", sa.Integer(), nullable=False),
        sa.Column("negative_count", sa.Integer(), nullable=False),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column("per_sample", sa.JSON(), nullable=False),
        sa.Column("gate_status", sa.String(length=16), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_benchmark_runs_benchmark_run_id"), "benchmark_runs", ["benchmark_run_id"], unique=True)
    for name in ("tenant_id", "corpus_digest", "evaluator_version", "gate_status"):
        op.create_index(op.f(f"ix_benchmark_runs_{name}"), "benchmark_runs", [name])

    op.create_table(
        "honeypot_probes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("probe_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("task_type", sa.String(length=32), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("package", sa.JSON(), nullable=False),
        sa.Column("conflict_test_ids", sa.JSON(), nullable=False),
        sa.Column("expected_honest_signals", sa.JSON(), nullable=False),
        sa.Column("builtin", sa.Boolean(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_honeypot_probes_probe_id"), "honeypot_probes", ["probe_id"], unique=True)
    op.create_index(op.f("ix_honeypot_probes_enabled"), "honeypot_probes", ["enabled"])

    op.create_table(
        "honeypot_launches",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("probe_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("agent_id", sa.String(length=128), nullable=False),
        sa.Column("agent_version", sa.String(length=128), nullable=False),
        sa.Column("model_name", sa.String(length=128), nullable=False),
        sa.Column("launched_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_honeypot_launches_run_id"), "honeypot_launches", ["run_id"], unique=True)
    for name in ("probe_id", "tenant_id", "agent_id"):
        op.create_index(op.f(f"ix_honeypot_launches_{name}"), "honeypot_launches", [name])

    op.create_table(
        "tenant_attestation_keys",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("key_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("algorithm", sa.String(length=32), nullable=False),
        sa.Column("public_key_pem", sa.Text(), nullable=False),
        sa.Column("key_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("allowed_subjects", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_tenant_attestation_keys_key_id"), "tenant_attestation_keys", ["key_id"], unique=True)
    for name in ("tenant_id", "key_fingerprint", "status"):
        op.create_index(op.f(f"ix_tenant_attestation_keys_{name}"), "tenant_attestation_keys", [name])


def downgrade() -> None:
    for name in ("status", "key_fingerprint", "tenant_id", "key_id"):
        op.drop_index(op.f(f"ix_tenant_attestation_keys_{name}"), table_name="tenant_attestation_keys")
    op.drop_table("tenant_attestation_keys")

    for name in ("agent_id", "tenant_id", "probe_id", "run_id"):
        op.drop_index(op.f(f"ix_honeypot_launches_{name}"), table_name="honeypot_launches")
    op.drop_table("honeypot_launches")

    for name in ("enabled", "probe_id"):
        op.drop_index(op.f(f"ix_honeypot_probes_{name}"), table_name="honeypot_probes")
    op.drop_table("honeypot_probes")

    for name in ("gate_status", "evaluator_version", "corpus_digest", "tenant_id", "benchmark_run_id"):
        op.drop_index(op.f(f"ix_benchmark_runs_{name}"), table_name="benchmark_runs")
    op.drop_table("benchmark_runs")

    for name in ("status", "origin_review_id", "label", "split", "source", "tenant_id", "scope", "corpus", "sample_id"):
        op.drop_index(op.f(f"ix_benchmark_samples_{name}"), table_name="benchmark_samples")
    op.drop_table("benchmark_samples")

    op.drop_column("evaluation_results", "suspicion_score")

    op.drop_column("evaluation_reviews", "finding_labels")
    for name, _column_type, _default, indexed in reversed(_REVIEW_COLUMNS):
        if indexed:
            op.drop_index(op.f(f"ix_evaluation_reviews_{name}"), table_name="evaluation_reviews")
        op.drop_column("evaluation_reviews", name)

    op.drop_column("run_outcomes", "attestation_detail")
    for name, _column_type, _default, indexed in reversed(_RUN_OUTCOME_COLUMNS):
        if indexed:
            op.drop_index(op.f(f"ix_run_outcomes_{name}"), table_name="run_outcomes")
        op.drop_column("run_outcomes", name)
