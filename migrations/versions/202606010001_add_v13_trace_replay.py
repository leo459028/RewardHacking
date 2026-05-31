"""add v1.3 trace replay audit rows

Revision ID: 202606010001
Revises: 202605310001
Create Date: 2026-06-01 10:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "202606010001"
down_revision = "202605310001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("execution_plans", sa.Column("trace_id", sa.String(length=128), nullable=False, server_default=""))
    op.add_column("execution_plans", sa.Column("trace_metadata", sa.JSON(), nullable=False, server_default="{}"))
    op.create_index(op.f("ix_execution_plans_trace_id"), "execution_plans", ["trace_id"])

    op.create_table(
        "execution_plan_steps",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("step_id", sa.String(length=128), nullable=False),
        sa.Column("plan_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("trace_id", sa.String(length=128), nullable=False),
        sa.Column("step_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("input_summary", sa.Text(), nullable=False),
        sa.Column("output_summary", sa.Text(), nullable=False),
        sa.Column("error_summary", sa.Text(), nullable=False),
        sa.Column("metadata_payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_execution_plan_steps_step_id"), "execution_plan_steps", ["step_id"])
    op.create_index(op.f("ix_execution_plan_steps_plan_id"), "execution_plan_steps", ["plan_id"])
    op.create_index(op.f("ix_execution_plan_steps_run_id"), "execution_plan_steps", ["run_id"])
    op.create_index(op.f("ix_execution_plan_steps_tenant_id"), "execution_plan_steps", ["tenant_id"])
    op.create_index(op.f("ix_execution_plan_steps_trace_id"), "execution_plan_steps", ["trace_id"])
    op.create_index(op.f("ix_execution_plan_steps_step_type"), "execution_plan_steps", ["step_type"])
    op.create_index(op.f("ix_execution_plan_steps_status"), "execution_plan_steps", ["status"])


def downgrade() -> None:
    op.drop_index(op.f("ix_execution_plan_steps_status"), table_name="execution_plan_steps")
    op.drop_index(op.f("ix_execution_plan_steps_step_type"), table_name="execution_plan_steps")
    op.drop_index(op.f("ix_execution_plan_steps_trace_id"), table_name="execution_plan_steps")
    op.drop_index(op.f("ix_execution_plan_steps_tenant_id"), table_name="execution_plan_steps")
    op.drop_index(op.f("ix_execution_plan_steps_run_id"), table_name="execution_plan_steps")
    op.drop_index(op.f("ix_execution_plan_steps_plan_id"), table_name="execution_plan_steps")
    op.drop_index(op.f("ix_execution_plan_steps_step_id"), table_name="execution_plan_steps")
    op.drop_table("execution_plan_steps")

    op.drop_index(op.f("ix_execution_plans_trace_id"), table_name="execution_plans")
    op.drop_column("execution_plans", "trace_metadata")
    op.drop_column("execution_plans", "trace_id")
