"""add v1.5.8 outcome evidence, post-execution review queue and evaluation policy

Revision ID: 202611070001
Revises: 202606010004
Create Date: 2026-11-07 10:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "202611070001"
down_revision = "202606010004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # evaluation_results: evidence-based evaluation fields (server defaults keep legacy rows valid)
    op.add_column("evaluation_results", sa.Column("tenant_id", sa.String(length=128), server_default="default", nullable=False))
    op.add_column("evaluation_results", sa.Column("evaluator_version", sa.String(length=64), server_default="", nullable=False))
    op.add_column("evaluation_results", sa.Column("evaluation_source", sa.String(length=32), server_default="executor", nullable=False))
    op.add_column("evaluation_results", sa.Column("assurance_level", sa.String(length=32), server_default="simulated", nullable=False))
    op.add_column("evaluation_results", sa.Column("findings", sa.JSON(), server_default=sa.text("'[]'"), nullable=False))
    op.add_column("evaluation_results", sa.Column("review_priority", sa.String(length=8), server_default="", nullable=False))
    op.add_column("evaluation_results", sa.Column("outcome_id", sa.String(length=64), server_default="", nullable=False))
    op.create_index(op.f("ix_evaluation_results_tenant_id"), "evaluation_results", ["tenant_id"])
    op.create_index(op.f("ix_evaluation_results_evaluation_source"), "evaluation_results", ["evaluation_source"])
    op.create_index(op.f("ix_evaluation_results_outcome_id"), "evaluation_results", ["outcome_id"])

    op.create_table(
        "run_outcomes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("outcome_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("submission_seq", sa.Integer(), nullable=False),
        sa.Column("provenance", sa.String(length=32), nullable=False),
        sa.Column("submitted_by", sa.String(length=128), nullable=False),
        sa.Column("submitted_by_key_id", sa.String(length=64), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("evidence_digest", sa.String(length=64), nullable=False),
        sa.Column("verdict", sa.String(length=32), nullable=False),
        sa.Column("review_priority", sa.String(length=8), nullable=False),
        sa.Column("assurance_level", sa.String(length=32), nullable=False),
        sa.Column("evaluator_version", sa.String(length=64), nullable=False),
        sa.Column("findings", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_run_outcomes_outcome_id"), "run_outcomes", ["outcome_id"], unique=True)
    op.create_index(op.f("ix_run_outcomes_run_id"), "run_outcomes", ["run_id"])
    op.create_index(op.f("ix_run_outcomes_tenant_id"), "run_outcomes", ["tenant_id"])
    op.create_index(op.f("ix_run_outcomes_provenance"), "run_outcomes", ["provenance"])
    op.create_index(op.f("ix_run_outcomes_verdict"), "run_outcomes", ["verdict"])

    op.create_table(
        "evaluation_reviews",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("review_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("priority", sa.String(length=8), nullable=False),
        sa.Column("categories", sa.JSON(), nullable=False),
        sa.Column("finding_ids", sa.JSON(), nullable=False),
        sa.Column("findings", sa.JSON(), nullable=False),
        sa.Column("sample_reason", sa.String(length=32), nullable=False),
        sa.Column("evaluator_version", sa.String(length=64), nullable=False),
        sa.Column("submitted_by_key_id", sa.String(length=64), nullable=False),
        sa.Column("submitted_by", sa.String(length=128), nullable=False),
        sa.Column("label", sa.String(length=32), nullable=False),
        sa.Column("label_categories", sa.JSON(), nullable=False),
        sa.Column("reviewer", sa.String(length=128), nullable=False),
        sa.Column("reviewer_key_id", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_evaluation_reviews_review_id"), "evaluation_reviews", ["review_id"], unique=True)
    op.create_index(op.f("ix_evaluation_reviews_run_id"), "evaluation_reviews", ["run_id"])
    op.create_index(op.f("ix_evaluation_reviews_tenant_id"), "evaluation_reviews", ["tenant_id"])
    op.create_index(op.f("ix_evaluation_reviews_status"), "evaluation_reviews", ["status"])
    op.create_index(op.f("ix_evaluation_reviews_priority"), "evaluation_reviews", ["priority"])
    op.create_index(op.f("ix_evaluation_reviews_label"), "evaluation_reviews", ["label"])

    op.create_table(
        "tenant_evaluation_policies",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("protected_paths", sa.JSON(), nullable=False),
        sa.Column("allow_default_protected_paths", sa.Boolean(), nullable=False),
        sa.Column("updated_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_tenant_evaluation_policies_tenant_id"), "tenant_evaluation_policies", ["tenant_id"], unique=True)


def downgrade() -> None:
    op.drop_index(op.f("ix_tenant_evaluation_policies_tenant_id"), table_name="tenant_evaluation_policies")
    op.drop_table("tenant_evaluation_policies")

    for name in ("label", "priority", "status", "tenant_id", "run_id", "review_id"):
        op.drop_index(op.f(f"ix_evaluation_reviews_{name}"), table_name="evaluation_reviews")
    op.drop_table("evaluation_reviews")

    for name in ("verdict", "provenance", "tenant_id", "run_id", "outcome_id"):
        op.drop_index(op.f(f"ix_run_outcomes_{name}"), table_name="run_outcomes")
    op.drop_table("run_outcomes")

    op.drop_index(op.f("ix_evaluation_results_outcome_id"), table_name="evaluation_results")
    op.drop_index(op.f("ix_evaluation_results_evaluation_source"), table_name="evaluation_results")
    op.drop_index(op.f("ix_evaluation_results_tenant_id"), table_name="evaluation_results")
    for column in (
        "outcome_id",
        "review_priority",
        "findings",
        "assurance_level",
        "evaluation_source",
        "evaluator_version",
        "tenant_id",
    ):
        op.drop_column("evaluation_results", column)
