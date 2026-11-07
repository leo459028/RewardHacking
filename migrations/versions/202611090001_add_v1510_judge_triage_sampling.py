"""add v1.5.10 judge triage assessments, review sampling decisions and triage columns

Revision ID: 202611090001
Revises: 202611080001
Create Date: 2026-11-09 09:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "202611090001"
down_revision = "202611080001"
branch_labels = None
depends_on = None

_REVIEW_COLUMNS = (
    ("triage_priority", sa.String(length=8)),
    ("triage_source", sa.String(length=200)),
    ("judge_assessment_id", sa.String(length=64)),
)


def upgrade() -> None:
    # Advisory triage columns on reviews. Server defaults keep existing rows valid without backfill.
    for name, column_type in _REVIEW_COLUMNS:
        op.add_column("evaluation_reviews", sa.Column(name, column_type, server_default="", nullable=False))
    op.add_column("evaluation_reviews", sa.Column("triage_score", sa.Float(), nullable=True))
    op.add_column("benchmark_runs", sa.Column("judge_config", sa.JSON(), server_default=sa.text("'{}'"), nullable=False))

    op.create_table(
        "judge_assessments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("assessment_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("review_id", sa.String(length=64), nullable=False),
        sa.Column("outcome_id", sa.String(length=64), nullable=False),
        sa.Column("benchmark_run_id", sa.String(length=64), nullable=False),
        sa.Column("sample_id", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("judge_version", sa.String(length=200), nullable=False),
        sa.Column("prompt_version", sa.String(length=16), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("suspicion", sa.Float(), nullable=True),
        sa.Column("triage_score", sa.Float(), nullable=False),
        sa.Column("triage_priority", sa.String(length=8), nullable=False),
        sa.Column("triage_source", sa.String(length=200), nullable=False),
        sa.Column("categories", sa.JSON(), nullable=False),
        sa.Column("evidence_refs", sa.JSON(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("fallback", sa.JSON(), nullable=False),
        sa.Column("injection_suspected", sa.Boolean(), nullable=False),
        sa.Column("injection_markers", sa.JSON(), nullable=False),
        sa.Column("same_family_warning", sa.Boolean(), nullable=False),
        sa.Column("input_digest", sa.String(length=64), nullable=False),
        sa.Column("input_truncated", sa.JSON(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("usage_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_judge_assessments_assessment_id"), "judge_assessments", ["assessment_id"], unique=True)
    for name in ("tenant_id", "run_id", "review_id", "benchmark_run_id", "provider", "judge_version", "status", "input_digest"):
        op.create_index(op.f(f"ix_judge_assessments_{name}"), "judge_assessments", [name])

    op.create_table(
        "review_sampling_decisions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("decision_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("outcome_id", sa.String(length=64), nullable=False),
        sa.Column("day", sa.String(length=10), nullable=False),
        sa.Column("slice", sa.String(length=16), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("skip_reason", sa.String(length=32), nullable=False),
        sa.Column("sampling_score", sa.Float(), nullable=False),
        sa.Column("random_draw", sa.Float(), nullable=False),
        sa.Column("also_threshold", sa.Boolean(), nullable=False),
        sa.Column("policy_version", sa.String(length=64), nullable=False),
        sa.Column("review_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_review_sampling_decisions_decision_id"), "review_sampling_decisions", ["decision_id"], unique=True)
    op.create_index(op.f("ix_review_sampling_decisions_run_id"), "review_sampling_decisions", ["run_id"], unique=True)
    for name in ("tenant_id", "day", "slice", "decision"):
        op.create_index(op.f(f"ix_review_sampling_decisions_{name}"), "review_sampling_decisions", [name])


def downgrade() -> None:
    for name in ("decision", "slice", "day", "tenant_id", "run_id", "decision_id"):
        op.drop_index(op.f(f"ix_review_sampling_decisions_{name}"), table_name="review_sampling_decisions")
    op.drop_table("review_sampling_decisions")

    for name in ("input_digest", "status", "judge_version", "provider", "benchmark_run_id", "review_id", "run_id", "tenant_id", "assessment_id"):
        op.drop_index(op.f(f"ix_judge_assessments_{name}"), table_name="judge_assessments")
    op.drop_table("judge_assessments")

    op.drop_column("benchmark_runs", "judge_config")
    op.drop_column("evaluation_reviews", "triage_score")
    for name, _column_type in reversed(_REVIEW_COLUMNS):
        op.drop_column("evaluation_reviews", name)
