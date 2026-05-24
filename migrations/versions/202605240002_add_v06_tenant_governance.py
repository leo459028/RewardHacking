"""add v0.6 tenant governance

Revision ID: 202605240002
Revises: 202605240001
Create Date: 2026-05-24 00:02:00
"""
from alembic import op
import sqlalchemy as sa

revision = "202605240002"
down_revision = "202605240001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_runs",
        sa.Column("tenant_id", sa.String(length=128), nullable=False, server_default="default"),
    )
    op.create_index(op.f("ix_agent_runs_tenant_id"), "agent_runs", ["tenant_id"], unique=False)
    op.alter_column("agent_runs", "tenant_id", server_default=None)

    op.create_table(
        "governance_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("limit_value", sa.Integer(), nullable=False),
        sa.Column("used", sa.Integer(), nullable=False),
        sa.Column("remaining", sa.Integer(), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_governance_events_tenant_id"), "governance_events", ["tenant_id"], unique=False)
    op.create_index(op.f("ix_governance_events_user_id"), "governance_events", ["user_id"], unique=False)
    op.create_index(op.f("ix_governance_events_run_id"), "governance_events", ["run_id"], unique=False)
    op.create_index(op.f("ix_governance_events_event_type"), "governance_events", ["event_type"], unique=False)
    op.create_index(op.f("ix_governance_events_decision"), "governance_events", ["decision"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_governance_events_decision"), table_name="governance_events")
    op.drop_index(op.f("ix_governance_events_event_type"), table_name="governance_events")
    op.drop_index(op.f("ix_governance_events_run_id"), table_name="governance_events")
    op.drop_index(op.f("ix_governance_events_user_id"), table_name="governance_events")
    op.drop_index(op.f("ix_governance_events_tenant_id"), table_name="governance_events")
    op.drop_table("governance_events")
    op.drop_index(op.f("ix_agent_runs_tenant_id"), table_name="agent_runs")
    op.drop_column("agent_runs", "tenant_id")
