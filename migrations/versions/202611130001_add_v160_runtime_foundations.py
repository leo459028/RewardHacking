"""add v1.6.0 runtime foundations: legal holds, retention purges (plan / receipt) and retention tombstones

Revision ID: 202611130001
Revises: 202611120001
Create Date: 2026-11-13 09:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "202611130001"
down_revision = "202611120001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # D15: legal holds on a tenant or a single run. Several active holds may cover one scope.
    op.create_table(
        "legal_holds",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("hold_id", sa.String(length=64), nullable=False),
        sa.Column("scope_type", sa.String(length=16), nullable=False),
        sa.Column("scope_id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), server_default="default", nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("created_by", sa.String(length=128), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("released_by", sa.String(length=128), server_default="", nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("release_reason", sa.Text(), server_default="", nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_legal_holds_hold_id"), "legal_holds", ["hold_id"], unique=True)
    op.create_index(op.f("ix_legal_holds_tenant_id"), "legal_holds", ["tenant_id"])
    op.create_index("ix_legal_holds_scope_status", "legal_holds", ["scope_type", "scope_id", "status"])

    # D11 / D13: one row per plan; the same row carries the apply receipt.
    op.create_table(
        "retention_purges",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("purge_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="planned", nullable=False),
        sa.Column("plan_digest", sa.String(length=64), nullable=False),
        sa.Column("cutoff_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settings_snapshot", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("candidate_run_ids", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("excluded_summary", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("estimated_counts", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("scan_summary", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("created_by", sa.String(length=128), server_default="", nullable=False),
        sa.Column("applied_by", sa.String(length=128), server_default="", nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_counts", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("skipped_at_apply", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("failures", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("receipt_digest", sa.String(length=64), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_retention_purges_purge_id"), "retention_purges", ["purge_id"], unique=True)
    op.create_index(op.f("ix_retention_purges_status"), "retention_purges", ["status"])
    op.create_index(op.f("ix_retention_purges_created_by"), "retention_purges", ["created_by"])

    # D13: hash-only tombstones. chain_version / prev_hash / entry_hash are reserved for a later hash chain.
    op.create_table(
        "retention_tombstones",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tombstone_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), server_default="default", nullable=False),
        sa.Column("purge_id", sa.String(length=64), nullable=False),
        sa.Column("purged_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("run_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("final_status", sa.String(length=32), server_default="", nullable=False),
        sa.Column("record_counts", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("content_digest", sa.String(length=64), nullable=False),
        sa.Column("policy_snapshot", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("chain_version", sa.String(length=16), server_default="", nullable=False),
        sa.Column("prev_hash", sa.String(length=64), nullable=True),
        sa.Column("entry_hash", sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_retention_tombstones_tombstone_id"), "retention_tombstones", ["tombstone_id"], unique=True)
    op.create_index(op.f("ix_retention_tombstones_run_id"), "retention_tombstones", ["run_id"], unique=True)
    op.create_index(op.f("ix_retention_tombstones_tenant_id"), "retention_tombstones", ["tenant_id"])
    op.create_index(op.f("ix_retention_tombstones_purge_id"), "retention_tombstones", ["purge_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_retention_tombstones_purge_id"), table_name="retention_tombstones")
    op.drop_index(op.f("ix_retention_tombstones_tenant_id"), table_name="retention_tombstones")
    op.drop_index(op.f("ix_retention_tombstones_run_id"), table_name="retention_tombstones")
    op.drop_index(op.f("ix_retention_tombstones_tombstone_id"), table_name="retention_tombstones")
    op.drop_table("retention_tombstones")
    op.drop_index(op.f("ix_retention_purges_created_by"), table_name="retention_purges")
    op.drop_index(op.f("ix_retention_purges_status"), table_name="retention_purges")
    op.drop_index(op.f("ix_retention_purges_purge_id"), table_name="retention_purges")
    op.drop_table("retention_purges")
    op.drop_index("ix_legal_holds_scope_status", table_name="legal_holds")
    op.drop_index(op.f("ix_legal_holds_tenant_id"), table_name="legal_holds")
    op.drop_index(op.f("ix_legal_holds_hold_id"), table_name="legal_holds")
    op.drop_table("legal_holds")
