"""add v1.2 control plane registry and execution plans

Revision ID: 202605310001
Revises: 202605250002
Create Date: 2026-05-31 17:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "202605310001"
down_revision = "202605250002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workflow_definitions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("workflow_id", sa.String(length=128), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("task_type", sa.String(length=32), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("definition", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_workflow_definitions_workflow_id"), "workflow_definitions", ["workflow_id"])
    op.create_index(op.f("ix_workflow_definitions_task_type"), "workflow_definitions", ["task_type"])
    op.create_index(op.f("ix_workflow_definitions_status"), "workflow_definitions", ["status"])

    op.create_table(
        "tool_definitions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tool_name", sa.String(length=128), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("task_types", sa.JSON(), nullable=False),
        sa.Column("requires_approval", sa.Boolean(), nullable=False),
        sa.Column("risk_level", sa.String(length=32), nullable=False),
        sa.Column("metadata_payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_tool_definitions_tool_name"), "tool_definitions", ["tool_name"], unique=True)
    op.create_index(op.f("ix_tool_definitions_status"), "tool_definitions", ["status"])

    op.create_table(
        "execution_plans",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("plan_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("workflow_id", sa.String(length=128), nullable=False),
        sa.Column("workflow_version", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("model_route", sa.JSON(), nullable=False),
        sa.Column("selected_tools", sa.JSON(), nullable=False),
        sa.Column("steps", sa.JSON(), nullable=False),
        sa.Column("approval_gates", sa.JSON(), nullable=False),
        sa.Column("policy_snapshot", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_execution_plans_plan_id"), "execution_plans", ["plan_id"], unique=True)
    op.create_index(op.f("ix_execution_plans_run_id"), "execution_plans", ["run_id"], unique=True)
    op.create_index(op.f("ix_execution_plans_tenant_id"), "execution_plans", ["tenant_id"])
    op.create_index(op.f("ix_execution_plans_workflow_id"), "execution_plans", ["workflow_id"])
    op.create_index(op.f("ix_execution_plans_status"), "execution_plans", ["status"])


def downgrade() -> None:
    op.drop_index(op.f("ix_execution_plans_status"), table_name="execution_plans")
    op.drop_index(op.f("ix_execution_plans_workflow_id"), table_name="execution_plans")
    op.drop_index(op.f("ix_execution_plans_tenant_id"), table_name="execution_plans")
    op.drop_index(op.f("ix_execution_plans_run_id"), table_name="execution_plans")
    op.drop_index(op.f("ix_execution_plans_plan_id"), table_name="execution_plans")
    op.drop_table("execution_plans")
    op.drop_index(op.f("ix_tool_definitions_status"), table_name="tool_definitions")
    op.drop_index(op.f("ix_tool_definitions_tool_name"), table_name="tool_definitions")
    op.drop_table("tool_definitions")
    op.drop_index(op.f("ix_workflow_definitions_status"), table_name="workflow_definitions")
    op.drop_index(op.f("ix_workflow_definitions_task_type"), table_name="workflow_definitions")
    op.drop_index(op.f("ix_workflow_definitions_workflow_id"), table_name="workflow_definitions")
    op.drop_table("workflow_definitions")
