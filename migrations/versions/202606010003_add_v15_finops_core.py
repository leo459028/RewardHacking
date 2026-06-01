"""add v1.5 finops core tables

Revision ID: 202606010003
Revises: 202606010002
Create Date: 2026-06-01 17:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "202606010003"
down_revision = "202606010002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pricing_catalog",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("pricing_id", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("resource_type", sa.String(length=64), nullable=False),
        sa.Column("model_name", sa.String(length=128), nullable=False),
        sa.Column("price_unit", sa.String(length=64), nullable=False),
        sa.Column("unit_price", sa.Numeric(18, 8), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("effective_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("metadata_payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_pricing_catalog_pricing_id"), "pricing_catalog", ["pricing_id"], unique=True)
    op.create_index(op.f("ix_pricing_catalog_provider"), "pricing_catalog", ["provider"])
    op.create_index(op.f("ix_pricing_catalog_resource_type"), "pricing_catalog", ["resource_type"])
    op.create_index(op.f("ix_pricing_catalog_model_name"), "pricing_catalog", ["model_name"])
    op.create_index(op.f("ix_pricing_catalog_price_unit"), "pricing_catalog", ["price_unit"])

    op.create_table(
        "usage_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("usage_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("trace_id", sa.String(length=128), nullable=False),
        sa.Column("workflow_id", sa.String(length=128), nullable=False),
        sa.Column("step_id", sa.String(length=128), nullable=False),
        sa.Column("checkpoint_id", sa.String(length=64), nullable=False),
        sa.Column("replay_attempt_id", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model_name", sa.String(length=128), nullable=False),
        sa.Column("resource_type", sa.String(length=64), nullable=False),
        sa.Column("usage_unit", sa.String(length=64), nullable=False),
        sa.Column("quantity", sa.Numeric(18, 6), nullable=False),
        sa.Column("phase", sa.String(length=32), nullable=False),
        sa.Column("metadata_payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_usage_events_usage_id"), "usage_events", ["usage_id"], unique=True)
    op.create_index(op.f("ix_usage_events_tenant_id"), "usage_events", ["tenant_id"])
    op.create_index(op.f("ix_usage_events_user_id"), "usage_events", ["user_id"])
    op.create_index(op.f("ix_usage_events_run_id"), "usage_events", ["run_id"])
    op.create_index(op.f("ix_usage_events_trace_id"), "usage_events", ["trace_id"])
    op.create_index(op.f("ix_usage_events_workflow_id"), "usage_events", ["workflow_id"])
    op.create_index(op.f("ix_usage_events_step_id"), "usage_events", ["step_id"])
    op.create_index(op.f("ix_usage_events_checkpoint_id"), "usage_events", ["checkpoint_id"])
    op.create_index(op.f("ix_usage_events_replay_attempt_id"), "usage_events", ["replay_attempt_id"])
    op.create_index(op.f("ix_usage_events_provider"), "usage_events", ["provider"])
    op.create_index(op.f("ix_usage_events_model_name"), "usage_events", ["model_name"])
    op.create_index(op.f("ix_usage_events_resource_type"), "usage_events", ["resource_type"])
    op.create_index(op.f("ix_usage_events_usage_unit"), "usage_events", ["usage_unit"])
    op.create_index(op.f("ix_usage_events_phase"), "usage_events", ["phase"])

    op.create_table(
        "cost_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("cost_id", sa.String(length=64), nullable=False),
        sa.Column("usage_event_id", sa.String(length=64), nullable=False),
        sa.Column("pricing_catalog_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("trace_id", sa.String(length=128), nullable=False),
        sa.Column("workflow_id", sa.String(length=128), nullable=False),
        sa.Column("resource_type", sa.String(length=64), nullable=False),
        sa.Column("model_name", sa.String(length=128), nullable=False),
        sa.Column("estimated_cost", sa.Numeric(18, 8), nullable=False),
        sa.Column("actual_cost", sa.Numeric(18, 8), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("allocation_tags", sa.JSON(), nullable=False),
        sa.Column("calculation_method", sa.String(length=32), nullable=False),
        sa.Column("metadata_payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_cost_events_cost_id"), "cost_events", ["cost_id"], unique=True)
    op.create_index(op.f("ix_cost_events_usage_event_id"), "cost_events", ["usage_event_id"])
    op.create_index(op.f("ix_cost_events_pricing_catalog_id"), "cost_events", ["pricing_catalog_id"])
    op.create_index(op.f("ix_cost_events_tenant_id"), "cost_events", ["tenant_id"])
    op.create_index(op.f("ix_cost_events_run_id"), "cost_events", ["run_id"])
    op.create_index(op.f("ix_cost_events_trace_id"), "cost_events", ["trace_id"])
    op.create_index(op.f("ix_cost_events_workflow_id"), "cost_events", ["workflow_id"])
    op.create_index(op.f("ix_cost_events_resource_type"), "cost_events", ["resource_type"])
    op.create_index(op.f("ix_cost_events_model_name"), "cost_events", ["model_name"])
    op.create_index(op.f("ix_cost_events_calculation_method"), "cost_events", ["calculation_method"])

    op.create_table(
        "budget_policies",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("budget_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("policy_name", sa.String(length=128), nullable=False),
        sa.Column("period", sa.String(length=32), nullable=False),
        sa.Column("limit_amount", sa.Numeric(18, 8), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("resource_type", sa.String(length=64), nullable=False),
        sa.Column("actions", sa.JSON(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("metadata_payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_budget_policies_budget_id"), "budget_policies", ["budget_id"], unique=True)
    op.create_index(op.f("ix_budget_policies_tenant_id"), "budget_policies", ["tenant_id"])
    op.create_index(op.f("ix_budget_policies_scope"), "budget_policies", ["scope"])
    op.create_index(op.f("ix_budget_policies_period"), "budget_policies", ["period"])
    op.create_index(op.f("ix_budget_policies_resource_type"), "budget_policies", ["resource_type"])

    op.create_table(
        "budget_violations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("violation_id", sa.String(length=64), nullable=False),
        sa.Column("budget_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("workflow_id", sa.String(length=128), nullable=False),
        sa.Column("model_name", sa.String(length=128), nullable=False),
        sa.Column("violation_type", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=32), nullable=False),
        sa.Column("actual_amount", sa.Numeric(18, 8), nullable=False),
        sa.Column("limit_amount", sa.Numeric(18, 8), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_budget_violations_violation_id"), "budget_violations", ["violation_id"], unique=True)
    op.create_index(op.f("ix_budget_violations_budget_id"), "budget_violations", ["budget_id"])
    op.create_index(op.f("ix_budget_violations_tenant_id"), "budget_violations", ["tenant_id"])
    op.create_index(op.f("ix_budget_violations_user_id"), "budget_violations", ["user_id"])
    op.create_index(op.f("ix_budget_violations_run_id"), "budget_violations", ["run_id"])
    op.create_index(op.f("ix_budget_violations_workflow_id"), "budget_violations", ["workflow_id"])
    op.create_index(op.f("ix_budget_violations_model_name"), "budget_violations", ["model_name"])
    op.create_index(op.f("ix_budget_violations_violation_type"), "budget_violations", ["violation_type"])
    op.create_index(op.f("ix_budget_violations_severity"), "budget_violations", ["severity"])
    op.create_index(op.f("ix_budget_violations_decision"), "budget_violations", ["decision"])


def downgrade() -> None:
    op.drop_index(op.f("ix_budget_violations_decision"), table_name="budget_violations")
    op.drop_index(op.f("ix_budget_violations_severity"), table_name="budget_violations")
    op.drop_index(op.f("ix_budget_violations_violation_type"), table_name="budget_violations")
    op.drop_index(op.f("ix_budget_violations_model_name"), table_name="budget_violations")
    op.drop_index(op.f("ix_budget_violations_workflow_id"), table_name="budget_violations")
    op.drop_index(op.f("ix_budget_violations_run_id"), table_name="budget_violations")
    op.drop_index(op.f("ix_budget_violations_user_id"), table_name="budget_violations")
    op.drop_index(op.f("ix_budget_violations_tenant_id"), table_name="budget_violations")
    op.drop_index(op.f("ix_budget_violations_budget_id"), table_name="budget_violations")
    op.drop_index(op.f("ix_budget_violations_violation_id"), table_name="budget_violations")
    op.drop_table("budget_violations")

    op.drop_index(op.f("ix_budget_policies_resource_type"), table_name="budget_policies")
    op.drop_index(op.f("ix_budget_policies_period"), table_name="budget_policies")
    op.drop_index(op.f("ix_budget_policies_scope"), table_name="budget_policies")
    op.drop_index(op.f("ix_budget_policies_tenant_id"), table_name="budget_policies")
    op.drop_index(op.f("ix_budget_policies_budget_id"), table_name="budget_policies")
    op.drop_table("budget_policies")

    op.drop_index(op.f("ix_cost_events_calculation_method"), table_name="cost_events")
    op.drop_index(op.f("ix_cost_events_model_name"), table_name="cost_events")
    op.drop_index(op.f("ix_cost_events_resource_type"), table_name="cost_events")
    op.drop_index(op.f("ix_cost_events_workflow_id"), table_name="cost_events")
    op.drop_index(op.f("ix_cost_events_trace_id"), table_name="cost_events")
    op.drop_index(op.f("ix_cost_events_run_id"), table_name="cost_events")
    op.drop_index(op.f("ix_cost_events_tenant_id"), table_name="cost_events")
    op.drop_index(op.f("ix_cost_events_pricing_catalog_id"), table_name="cost_events")
    op.drop_index(op.f("ix_cost_events_usage_event_id"), table_name="cost_events")
    op.drop_index(op.f("ix_cost_events_cost_id"), table_name="cost_events")
    op.drop_table("cost_events")

    op.drop_index(op.f("ix_usage_events_phase"), table_name="usage_events")
    op.drop_index(op.f("ix_usage_events_usage_unit"), table_name="usage_events")
    op.drop_index(op.f("ix_usage_events_resource_type"), table_name="usage_events")
    op.drop_index(op.f("ix_usage_events_model_name"), table_name="usage_events")
    op.drop_index(op.f("ix_usage_events_provider"), table_name="usage_events")
    op.drop_index(op.f("ix_usage_events_replay_attempt_id"), table_name="usage_events")
    op.drop_index(op.f("ix_usage_events_checkpoint_id"), table_name="usage_events")
    op.drop_index(op.f("ix_usage_events_step_id"), table_name="usage_events")
    op.drop_index(op.f("ix_usage_events_workflow_id"), table_name="usage_events")
    op.drop_index(op.f("ix_usage_events_trace_id"), table_name="usage_events")
    op.drop_index(op.f("ix_usage_events_run_id"), table_name="usage_events")
    op.drop_index(op.f("ix_usage_events_user_id"), table_name="usage_events")
    op.drop_index(op.f("ix_usage_events_tenant_id"), table_name="usage_events")
    op.drop_index(op.f("ix_usage_events_usage_id"), table_name="usage_events")
    op.drop_table("usage_events")

    op.drop_index(op.f("ix_pricing_catalog_price_unit"), table_name="pricing_catalog")
    op.drop_index(op.f("ix_pricing_catalog_model_name"), table_name="pricing_catalog")
    op.drop_index(op.f("ix_pricing_catalog_resource_type"), table_name="pricing_catalog")
    op.drop_index(op.f("ix_pricing_catalog_provider"), table_name="pricing_catalog")
    op.drop_index(op.f("ix_pricing_catalog_pricing_id"), table_name="pricing_catalog")
    op.drop_table("pricing_catalog")
