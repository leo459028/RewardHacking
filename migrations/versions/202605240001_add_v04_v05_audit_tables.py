"""add v0.4 v0.5 audit tables

Revision ID: 202605240001
Revises: 202605190001
Create Date: 2026-05-24 00:01:00
"""
from alembic import op
import sqlalchemy as sa

revision = "202605240001"
down_revision = "202605190001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "worker_attempts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=False),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_worker_attempts_run_id"), "worker_attempts", ["run_id"], unique=False)
    op.create_index(op.f("ix_worker_attempts_status"), "worker_attempts", ["status"], unique=False)

    op.create_table(
        "tool_executions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("tool_name", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("sandbox_policy", sa.JSON(), nullable=False),
        sa.Column("artifact_refs", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_tool_executions_run_id"), "tool_executions", ["run_id"], unique=False)
    op.create_index(op.f("ix_tool_executions_tool_name"), "tool_executions", ["tool_name"], unique=False)

    op.create_table(
        "approval_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("reviewer", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expired", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_approval_events_run_id"), "approval_events", ["run_id"], unique=False)
    op.create_index(op.f("ix_approval_events_status"), "approval_events", ["status"], unique=False)
    op.create_index(op.f("ix_approval_events_reviewer"), "approval_events", ["reviewer"], unique=False)

    op.create_table(
        "evaluation_results",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("task_type", sa.String(length=32), nullable=False),
        sa.Column("verdict", sa.String(length=32), nullable=False),
        sa.Column("final_score", sa.Float(), nullable=False),
        sa.Column("rubric", sa.JSON(), nullable=False),
        sa.Column("judge_signal", sa.JSON(), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_evaluation_results_run_id"), "evaluation_results", ["run_id"], unique=False)
    op.create_index(op.f("ix_evaluation_results_task_type"), "evaluation_results", ["task_type"], unique=False)
    op.create_index(op.f("ix_evaluation_results_verdict"), "evaluation_results", ["verdict"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_evaluation_results_verdict"), table_name="evaluation_results")
    op.drop_index(op.f("ix_evaluation_results_task_type"), table_name="evaluation_results")
    op.drop_index(op.f("ix_evaluation_results_run_id"), table_name="evaluation_results")
    op.drop_table("evaluation_results")
    op.drop_index(op.f("ix_approval_events_reviewer"), table_name="approval_events")
    op.drop_index(op.f("ix_approval_events_status"), table_name="approval_events")
    op.drop_index(op.f("ix_approval_events_run_id"), table_name="approval_events")
    op.drop_table("approval_events")
    op.drop_index(op.f("ix_tool_executions_tool_name"), table_name="tool_executions")
    op.drop_index(op.f("ix_tool_executions_run_id"), table_name="tool_executions")
    op.drop_table("tool_executions")
    op.drop_index(op.f("ix_worker_attempts_status"), table_name="worker_attempts")
    op.drop_index(op.f("ix_worker_attempts_run_id"), table_name="worker_attempts")
    op.drop_table("worker_attempts")
