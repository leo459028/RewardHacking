"""create audit tables

Revision ID: 202605190001
Revises:
Create Date: 2026-05-19 00:01:00
"""
from alembic import op
import sqlalchemy as sa

revision = "202605190001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("task_type", sa.String(length=32), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("decision_reason", sa.Text(), nullable=False),
        sa.Column("risk_score", sa.Float(), nullable=False),
        sa.Column("policy_flags", sa.JSON(), nullable=False),
        sa.Column("request_payload", sa.JSON(), nullable=False),
        sa.Column("execution_output", sa.Text(), nullable=False),
        sa.Column("evaluation_summary", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("requires_human_review", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_agent_runs_run_id"), "agent_runs", ["run_id"], unique=True)
    op.create_index(op.f("ix_agent_runs_status"), "agent_runs", ["status"], unique=False)
    op.create_index(op.f("ix_agent_runs_user_id"), "agent_runs", ["user_id"], unique=False)

    op.create_table(
        "incidents",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_incidents_run_id"), "incidents", ["run_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_incidents_run_id"), table_name="incidents")
    op.drop_table("incidents")
    op.drop_index(op.f("ix_agent_runs_user_id"), table_name="agent_runs")
    op.drop_index(op.f("ix_agent_runs_status"), table_name="agent_runs")
    op.drop_index(op.f("ix_agent_runs_run_id"), table_name="agent_runs")
    op.drop_table("agent_runs")
