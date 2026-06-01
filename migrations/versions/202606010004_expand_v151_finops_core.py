"""expand v1.5.1 finops core

Revision ID: 202606010004
Revises: 202606010003
Create Date: 2026-06-01 23:40:00
"""
from alembic import op
import sqlalchemy as sa

revision = "202606010004"
down_revision = "202606010003"
branch_labels = None
depends_on = None


def _add_columns(table_name: str, columns: list[sa.Column]) -> None:
    for column in columns:
        op.add_column(table_name, column)


def _drop_columns(table_name: str, column_names: list[str]) -> None:
    for column_name in reversed(column_names):
        op.drop_column(table_name, column_name)


def upgrade() -> None:
    _add_columns(
        "pricing_catalog",
        [
            sa.Column("provider_display_name", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("billing_platform", sa.String(length=64), nullable=False, server_default="direct_api"),
            sa.Column("provider_region", sa.String(length=64), nullable=False, server_default="global"),
            sa.Column("billing_region", sa.String(length=64), nullable=False, server_default="global"),
            sa.Column("resource_subtype", sa.String(length=64), nullable=False, server_default="other"),
            sa.Column("model_alias", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("model_version", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("model_family", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("model_tier", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("modality", sa.String(length=32), nullable=False, server_default="text"),
            sa.Column("pricing_mode", sa.String(length=64), nullable=False, server_default="standard"),
            sa.Column("service_tier", sa.String(length=64), nullable=False, server_default="standard"),
            sa.Column("token_type", sa.String(length=64), nullable=False, server_default="not_applicable"),
            sa.Column("tax_included", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("discount_rate", sa.Numeric(18, 8), nullable=False, server_default="0"),
            sa.Column("multiplier", sa.Numeric(18, 8), nullable=False, server_default="1"),
            sa.Column("minimum_billable_quantity", sa.Numeric(24, 8), nullable=False, server_default="0"),
            sa.Column("free_quota_quantity", sa.Numeric(24, 8), nullable=False, server_default="0"),
            sa.Column("free_quota_period", sa.String(length=32), nullable=False, server_default=""),
            sa.Column("input_threshold_tokens_min", sa.Integer(), nullable=True),
            sa.Column("input_threshold_tokens_max", sa.Integer(), nullable=True),
            sa.Column("context_window_tokens", sa.Integer(), nullable=True),
            sa.Column("max_output_tokens", sa.Integer(), nullable=True),
            sa.Column("cache_ttl_seconds", sa.Integer(), nullable=True),
            sa.Column("cache_storage_unit_price", sa.Numeric(24, 12), nullable=False, server_default="0"),
            sa.Column("supports_cache", sa.Boolean(), nullable=True),
            sa.Column("supports_batch", sa.Boolean(), nullable=True),
            sa.Column("supports_tools", sa.Boolean(), nullable=True),
            sa.Column("supports_thinking", sa.Boolean(), nullable=True),
            sa.Column("supports_streaming", sa.Boolean(), nullable=True),
            sa.Column("supports_json_mode", sa.Boolean(), nullable=True),
            sa.Column("supports_multimodal", sa.Boolean(), nullable=True),
            sa.Column("source_url", sa.Text(), nullable=False, server_default="manual://migration"),
            sa.Column("source_type", sa.String(length=64), nullable=False, server_default="manual"),
            sa.Column("source_confidence", sa.String(length=64), nullable=False, server_default="manual"),
            sa.Column("source_checked_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("pricing_version", sa.String(length=64), nullable=False, server_default="manual"),
            sa.Column("scope_type", sa.String(length=64), nullable=False, server_default="manual"),
            sa.Column("scope_id", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("is_system_default", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("is_tenant_override", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("supersedes_pricing_id", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("import_batch_id", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("change_reason", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_by", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("updated_by", sa.String(length=128), nullable=False, server_default=""),
        ],
    )
    op.create_index("ix_pricing_catalog_billing_platform", "pricing_catalog", ["billing_platform"])
    op.create_index("ix_pricing_catalog_provider_region", "pricing_catalog", ["provider_region"])
    op.create_index("ix_pricing_catalog_resource_subtype", "pricing_catalog", ["resource_subtype"])
    op.create_index("ix_pricing_catalog_model_version", "pricing_catalog", ["model_version"])
    op.create_index("ix_pricing_catalog_model_family", "pricing_catalog", ["model_family"])
    op.create_index("ix_pricing_catalog_model_tier", "pricing_catalog", ["model_tier"])
    op.create_index("ix_pricing_catalog_pricing_mode", "pricing_catalog", ["pricing_mode"])
    op.create_index("ix_pricing_catalog_token_type", "pricing_catalog", ["token_type"])
    op.create_index("ix_pricing_catalog_scope_type", "pricing_catalog", ["scope_type"])
    op.create_index("ix_pricing_catalog_scope_id", "pricing_catalog", ["scope_id"])
    op.create_index("ix_pricing_catalog_supersedes_pricing_id", "pricing_catalog", ["supersedes_pricing_id"])
    op.create_index("ix_pricing_catalog_import_batch_id", "pricing_catalog", ["import_batch_id"])

    _add_columns(
        "usage_events",
        [
            sa.Column("org_id", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("project_id", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("environment", sa.String(length=32), nullable=False, server_default="dev"),
            sa.Column("cost_center", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("workflow_version", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("attempt_id", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("span_id", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("resume_attempt_id", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("billing_platform", sa.String(length=64), nullable=False, server_default="direct_api"),
            sa.Column("provider_request_id", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("provider_response_id", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("model_version", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("pricing_id", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("resource_subtype", sa.String(length=64), nullable=False, server_default="other"),
            sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("cached_input_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("cache_write_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("reasoning_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("audio_input_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("audio_output_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("image_input_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("image_output_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("video_input_seconds", sa.Numeric(24, 8), nullable=False, server_default="0"),
            sa.Column("tool_id", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("tool_name", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("tool_provider", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("tool_call_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("mcp_server_id", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("tool_risk_level", sa.String(length=32), nullable=False, server_default=""),
            sa.Column("dry_run", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("external_side_effect", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("container_seconds", sa.Numeric(24, 8), nullable=False, server_default="0"),
            sa.Column("worker_seconds", sa.Numeric(24, 8), nullable=False, server_default="0"),
            sa.Column("storage_gb_seconds", sa.Numeric(24, 8), nullable=False, server_default="0"),
            sa.Column("network_egress_gb", sa.Numeric(24, 8), nullable=False, server_default="0"),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="success"),
            sa.Column("error_code", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("error_message_hash", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("queue_wait_ms", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("idempotency_key", sa.String(length=128), nullable=False, server_default=""),
        ],
    )

    _add_columns(
        "cost_events",
        [
            sa.Column("pricing_snapshot", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("org_id", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("project_id", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("cost_center", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("environment", sa.String(length=32), nullable=False, server_default="dev"),
            sa.Column("user_id", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("step_id", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("checkpoint_id", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("replay_attempt_id", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("resume_attempt_id", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("provider", sa.String(length=64), nullable=False, server_default="internal"),
            sa.Column("billing_platform", sa.String(length=64), nullable=False, server_default="direct_api"),
            sa.Column("resource_subtype", sa.String(length=64), nullable=False, server_default="other"),
            sa.Column("model_version", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("cost_type", sa.String(length=32), nullable=False, server_default="estimated"),
            sa.Column("usage_unit", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("quantity", sa.Numeric(24, 8), nullable=False, server_default="0"),
            sa.Column("unit_price", sa.Numeric(24, 12), nullable=False, server_default="0"),
            sa.Column("gross_cost", sa.Numeric(24, 12), nullable=False, server_default="0"),
            sa.Column("discount_rate", sa.Numeric(18, 8), nullable=False, server_default="0"),
            sa.Column("discount_amount", sa.Numeric(24, 12), nullable=False, server_default="0"),
            sa.Column("multiplier", sa.Numeric(18, 8), nullable=False, server_default="1"),
            sa.Column("net_cost", sa.Numeric(24, 12), nullable=False, server_default="0"),
            sa.Column("tax_amount", sa.Numeric(24, 12), nullable=False, server_default="0"),
            sa.Column("base_currency", sa.String(length=8), nullable=False, server_default="USD"),
            sa.Column("base_currency_cost", sa.Numeric(24, 12), nullable=False, server_default="0"),
            sa.Column("fx_rate", sa.Numeric(18, 8), nullable=False, server_default="1"),
            sa.Column("fx_rate_source", sa.String(length=128), nullable=False, server_default="same_currency"),
            sa.Column("billing_period_start", sa.DateTime(timezone=True), nullable=True),
            sa.Column("billing_period_end", sa.DateTime(timezone=True), nullable=True),
            sa.Column("provider_invoice_id", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("calculation_version", sa.String(length=32), nullable=False, server_default="v1.5.1"),
        ],
    )

    _add_columns(
        "budget_policies",
        [
            sa.Column("scope_id", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("project_id", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("environment", sa.String(length=32), nullable=False, server_default="prod"),
            sa.Column("policy_version", sa.String(length=64), nullable=False, server_default="1"),
            sa.Column("period_start", sa.DateTime(timezone=True), nullable=True),
            sa.Column("period_end", sa.DateTime(timezone=True), nullable=True),
            sa.Column("timezone", sa.String(length=64), nullable=False, server_default="UTC"),
            sa.Column("soft_limit_amount", sa.Numeric(24, 12), nullable=False, server_default="0"),
            sa.Column("hard_limit_amount", sa.Numeric(24, 12), nullable=False, server_default="0"),
            sa.Column("base_currency", sa.String(length=8), nullable=False, server_default="USD"),
            sa.Column("provider", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("model_name", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("model_tier", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("tool_id", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("workflow_id", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("budget_basis", sa.String(length=32), nullable=False, server_default="actual"),
            sa.Column("include_failed_runs", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("include_replay_runs", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("include_dev_environment", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("actions_on_soft_limit", sa.JSON(), nullable=False, server_default="[]"),
            sa.Column("actions_on_hard_limit", sa.JSON(), nullable=False, server_default="[]"),
            sa.Column("notification_channels", sa.JSON(), nullable=False, server_default="[]"),
            sa.Column("approval_required", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("approver_role", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("fallback_model_tier", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("effective_from", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("effective_to", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_by", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("updated_by", sa.String(length=128), nullable=False, server_default=""),
        ],
    )

    _add_columns(
        "budget_violations",
        [
            sa.Column("budget_policy_version", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("scope", sa.String(length=32), nullable=False, server_default=""),
            sa.Column("scope_id", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("project_id", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("environment", sa.String(length=32), nullable=False, server_default=""),
            sa.Column("step_id", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("provider", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("resource_type", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("threshold_type", sa.String(length=32), nullable=False, server_default="hard"),
            sa.Column("estimated_amount", sa.Numeric(24, 12), nullable=False, server_default="0"),
            sa.Column("remaining_budget_before", sa.Numeric(24, 12), nullable=False, server_default="0"),
            sa.Column("overage_amount", sa.Numeric(24, 12), nullable=False, server_default="0"),
            sa.Column("overage_percent", sa.Numeric(18, 8), nullable=False, server_default="0"),
            sa.Column("recommended_action", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("action_taken", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("approval_id", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("acknowledged_by", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("resolved_by", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("resolution_status", sa.String(length=32), nullable=False, server_default="open"),
        ],
    )

    op.create_table(
        "pricing_import_batches",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("import_batch_id", sa.String(length=64), nullable=False),
        sa.Column("source_file_name", sa.String(length=256), nullable=False),
        sa.Column("source_file_hash", sa.String(length=128), nullable=False),
        sa.Column("import_mode", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("error_details", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_pricing_import_batches_import_batch_id", "pricing_import_batches", ["import_batch_id"], unique=True)
    op.create_index("ix_pricing_import_batches_source_file_hash", "pricing_import_batches", ["source_file_hash"])

    op.create_table(
        "pricing_change_logs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("change_id", sa.String(length=64), nullable=False),
        sa.Column("pricing_id_old", sa.String(length=64), nullable=False),
        sa.Column("pricing_id_new", sa.String(length=64), nullable=False),
        sa.Column("change_type", sa.String(length=32), nullable=False),
        sa.Column("old_unit_price", sa.Numeric(24, 12), nullable=True),
        sa.Column("new_unit_price", sa.Numeric(24, 12), nullable=True),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("change_reason", sa.Text(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("changed_by", sa.String(length=128), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("import_batch_id", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_pricing_change_logs_change_id", "pricing_change_logs", ["change_id"], unique=True)
    op.create_index("ix_pricing_change_logs_pricing_id_old", "pricing_change_logs", ["pricing_id_old"])
    op.create_index("ix_pricing_change_logs_pricing_id_new", "pricing_change_logs", ["pricing_id_new"])
    op.create_index("ix_pricing_change_logs_import_batch_id", "pricing_change_logs", ["import_batch_id"])

    op.create_table(
        "pricing_source_documents",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("source_type", sa.String(length=64), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(length=128), nullable=False),
        sa.Column("confidence", sa.String(length=64), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_pricing_source_documents_source_id", "pricing_source_documents", ["source_id"], unique=True)
    op.create_index("ix_pricing_source_documents_provider", "pricing_source_documents", ["provider"])
    op.create_index("ix_pricing_source_documents_source_type", "pricing_source_documents", ["source_type"])
    op.create_index("ix_pricing_source_documents_content_hash", "pricing_source_documents", ["content_hash"])


def downgrade() -> None:
    op.drop_index("ix_pricing_source_documents_content_hash", table_name="pricing_source_documents")
    op.drop_index("ix_pricing_source_documents_source_type", table_name="pricing_source_documents")
    op.drop_index("ix_pricing_source_documents_provider", table_name="pricing_source_documents")
    op.drop_index("ix_pricing_source_documents_source_id", table_name="pricing_source_documents")
    op.drop_table("pricing_source_documents")

    op.drop_index("ix_pricing_change_logs_import_batch_id", table_name="pricing_change_logs")
    op.drop_index("ix_pricing_change_logs_pricing_id_new", table_name="pricing_change_logs")
    op.drop_index("ix_pricing_change_logs_pricing_id_old", table_name="pricing_change_logs")
    op.drop_index("ix_pricing_change_logs_change_id", table_name="pricing_change_logs")
    op.drop_table("pricing_change_logs")

    op.drop_index("ix_pricing_import_batches_source_file_hash", table_name="pricing_import_batches")
    op.drop_index("ix_pricing_import_batches_import_batch_id", table_name="pricing_import_batches")
    op.drop_table("pricing_import_batches")

    _drop_columns(
        "budget_violations",
        [
            "budget_policy_version",
            "scope",
            "scope_id",
            "project_id",
            "environment",
            "step_id",
            "provider",
            "resource_type",
            "threshold_type",
            "estimated_amount",
            "remaining_budget_before",
            "overage_amount",
            "overage_percent",
            "recommended_action",
            "action_taken",
            "approval_id",
            "acknowledged_by",
            "acknowledged_at",
            "resolved_by",
            "resolved_at",
            "resolution_status",
        ],
    )

    _drop_columns(
        "budget_policies",
        [
            "scope_id",
            "project_id",
            "environment",
            "policy_version",
            "period_start",
            "period_end",
            "timezone",
            "soft_limit_amount",
            "hard_limit_amount",
            "base_currency",
            "provider",
            "model_name",
            "model_tier",
            "tool_id",
            "workflow_id",
            "budget_basis",
            "include_failed_runs",
            "include_replay_runs",
            "include_dev_environment",
            "actions_on_soft_limit",
            "actions_on_hard_limit",
            "notification_channels",
            "approval_required",
            "approver_role",
            "fallback_model_tier",
            "effective_from",
            "effective_to",
            "created_by",
            "updated_by",
        ],
    )

    _drop_columns(
        "cost_events",
        [
            "pricing_snapshot",
            "org_id",
            "project_id",
            "cost_center",
            "environment",
            "user_id",
            "step_id",
            "checkpoint_id",
            "replay_attempt_id",
            "resume_attempt_id",
            "provider",
            "billing_platform",
            "resource_subtype",
            "model_version",
            "cost_type",
            "usage_unit",
            "quantity",
            "unit_price",
            "gross_cost",
            "discount_rate",
            "discount_amount",
            "multiplier",
            "net_cost",
            "tax_amount",
            "base_currency",
            "base_currency_cost",
            "fx_rate",
            "fx_rate_source",
            "billing_period_start",
            "billing_period_end",
            "provider_invoice_id",
            "calculation_version",
        ],
    )

    _drop_columns(
        "usage_events",
        [
            "org_id",
            "project_id",
            "environment",
            "cost_center",
            "workflow_version",
            "attempt_id",
            "span_id",
            "resume_attempt_id",
            "billing_platform",
            "provider_request_id",
            "provider_response_id",
            "model_version",
            "pricing_id",
            "resource_subtype",
            "input_tokens",
            "output_tokens",
            "cached_input_tokens",
            "cache_write_tokens",
            "reasoning_tokens",
            "audio_input_tokens",
            "audio_output_tokens",
            "image_input_count",
            "image_output_count",
            "video_input_seconds",
            "tool_id",
            "tool_name",
            "tool_provider",
            "tool_call_count",
            "mcp_server_id",
            "tool_risk_level",
            "dry_run",
            "external_side_effect",
            "container_seconds",
            "worker_seconds",
            "storage_gb_seconds",
            "network_egress_gb",
            "status",
            "error_code",
            "error_message_hash",
            "latency_ms",
            "queue_wait_ms",
            "retry_count",
            "idempotency_key",
        ],
    )

    op.drop_index("ix_pricing_catalog_import_batch_id", table_name="pricing_catalog")
    op.drop_index("ix_pricing_catalog_supersedes_pricing_id", table_name="pricing_catalog")
    op.drop_index("ix_pricing_catalog_scope_id", table_name="pricing_catalog")
    op.drop_index("ix_pricing_catalog_scope_type", table_name="pricing_catalog")
    op.drop_index("ix_pricing_catalog_token_type", table_name="pricing_catalog")
    op.drop_index("ix_pricing_catalog_pricing_mode", table_name="pricing_catalog")
    op.drop_index("ix_pricing_catalog_model_tier", table_name="pricing_catalog")
    op.drop_index("ix_pricing_catalog_model_family", table_name="pricing_catalog")
    op.drop_index("ix_pricing_catalog_model_version", table_name="pricing_catalog")
    op.drop_index("ix_pricing_catalog_resource_subtype", table_name="pricing_catalog")
    op.drop_index("ix_pricing_catalog_provider_region", table_name="pricing_catalog")
    op.drop_index("ix_pricing_catalog_billing_platform", table_name="pricing_catalog")
    _drop_columns(
        "pricing_catalog",
        [
            "provider_display_name",
            "billing_platform",
            "provider_region",
            "billing_region",
            "resource_subtype",
            "model_alias",
            "model_version",
            "model_family",
            "model_tier",
            "modality",
            "pricing_mode",
            "service_tier",
            "token_type",
            "tax_included",
            "discount_rate",
            "multiplier",
            "minimum_billable_quantity",
            "free_quota_quantity",
            "free_quota_period",
            "input_threshold_tokens_min",
            "input_threshold_tokens_max",
            "context_window_tokens",
            "max_output_tokens",
            "cache_ttl_seconds",
            "cache_storage_unit_price",
            "supports_cache",
            "supports_batch",
            "supports_tools",
            "supports_thinking",
            "supports_streaming",
            "supports_json_mode",
            "supports_multimodal",
            "source_url",
            "source_type",
            "source_confidence",
            "source_checked_at",
            "pricing_version",
            "scope_type",
            "scope_id",
            "is_system_default",
            "is_tenant_override",
            "supersedes_pricing_id",
            "import_batch_id",
            "change_reason",
            "created_by",
            "updated_by",
        ],
    )
