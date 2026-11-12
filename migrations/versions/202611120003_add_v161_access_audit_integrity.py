"""add v1.6.1 access and audit integrity: dashboard users and sessions, change requests, audit chain

Revision ID: 202611120003
Revises: 202611120002
Create Date: 2026-11-11 16:20:00
"""
from alembic import op
import sqlalchemy as sa

revision = "202611120003"
down_revision = "202611120002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # D11: personal Dashboard accounts. tenant_id NULL = platform user (D22); external_subject reserved for OIDC (D16).
    op.create_table(
        "dashboard_users",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=128), server_default="", nullable=False),
        sa.Column("password_hash", sa.String(length=255), server_default="", nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("auth_source", sa.String(length=32), server_default="local", nullable=False),
        sa.Column("external_subject", sa.String(length=255), nullable=True),
        sa.Column("failed_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("must_change_password", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(length=128), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_dashboard_users_user_id"), "dashboard_users", ["user_id"], unique=True)
    op.create_index(op.f("ix_dashboard_users_username"), "dashboard_users", ["username"], unique=True)
    op.create_index(op.f("ix_dashboard_users_tenant_id"), "dashboard_users", ["tenant_id"])
    op.create_index("ux_dashboard_users_external", "dashboard_users", ["auth_source", "external_subject"], unique=True)

    # D15: server-side sessions; only the SHA-256 of the cookie token is stored.
    op.create_table(
        "dashboard_sessions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("session_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("auth_method", sa.String(length=32), server_default="local_password", nullable=False),
        sa.Column("csrf_secret", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reauth_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(length=32), server_default="", nullable=False),
        sa.Column("user_agent_hash", sa.String(length=64), server_default="", nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_dashboard_sessions_session_id"), "dashboard_sessions", ["session_id"], unique=True)
    op.create_index(op.f("ix_dashboard_sessions_session_hash"), "dashboard_sessions", ["session_hash"], unique=True)
    op.create_index(op.f("ix_dashboard_sessions_user_id"), "dashboard_sessions", ["user_id"])

    # D37: dual-control change requests (service arrives in P6).
    op.create_table(
        "change_requests",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("change_id", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target_ref", sa.String(length=255), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=True),
        sa.Column("payload", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("payload_digest", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("requested_by", sa.String(length=128), nullable=False),
        sa.Column("requested_reason", sa.Text(), server_default="", nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_by", sa.String(length=128), server_default="", nullable=False),
        sa.Column("decision_reason", sa.Text(), server_default="", nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("error_code", sa.String(length=64), server_default="", nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_change_requests_change_id"), "change_requests", ["change_id"], unique=True)
    op.create_index(op.f("ix_change_requests_tenant_id"), "change_requests", ["tenant_id"])
    op.create_index(op.f("ix_change_requests_status"), "change_requests", ["status"])
    op.create_index("ix_change_requests_target_status", "change_requests", ["action", "target_ref", "status"])

    # D28: separate hash ledger, never touched by retention purges. seq is assigned by the writer (part of entry_hash).
    op.create_table(
        "audit_chain_entries",
        sa.Column("seq", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("entry_id", sa.String(length=64), nullable=False),
        sa.Column("entry_type", sa.String(length=32), nullable=False),
        sa.Column("ref_table", sa.String(length=64), server_default="", nullable=False),
        sa.Column("ref_id", sa.String(length=128), server_default="", nullable=False),
        sa.Column("tenant_id", sa.String(length=128), server_default="", nullable=False),
        sa.Column("run_id", sa.String(length=64), server_default="", nullable=False),
        sa.Column("payload_digest", sa.String(length=64), nullable=False),
        sa.Column("prev_hash", sa.String(length=64), nullable=False),
        sa.Column("entry_hash", sa.String(length=64), nullable=False),
        sa.Column("chain_version", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("seq"),
        sa.UniqueConstraint("entry_hash"),
    )
    op.create_index(op.f("ix_audit_chain_entries_entry_id"), "audit_chain_entries", ["entry_id"], unique=True)
    op.create_index(op.f("ix_audit_chain_entries_entry_type"), "audit_chain_entries", ["entry_type"])
    op.create_index(op.f("ix_audit_chain_entries_tenant_id"), "audit_chain_entries", ["tenant_id"])
    op.create_index(op.f("ix_audit_chain_entries_run_id"), "audit_chain_entries", ["run_id"])
    op.create_index("ix_audit_chain_entries_ref", "audit_chain_entries", ["ref_table", "ref_id"])

    # D33: signed (Ed25519) or unsigned statements of the chain head.
    op.create_table(
        "audit_chain_checkpoints",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("checkpoint_id", sa.String(length=64), nullable=False),
        sa.Column("through_seq", sa.BigInteger(), nullable=False),
        sa.Column("head_hash", sa.String(length=64), nullable=False),
        sa.Column("entry_count", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("prev_checkpoint_hash", sa.String(length=64), server_default="", nullable=False),
        sa.Column("checkpoint_hash", sa.String(length=64), server_default="", nullable=False),
        sa.Column("algorithm", sa.String(length=16), server_default="ed25519", nullable=False),
        sa.Column("key_id", sa.String(length=16), server_default="", nullable=False),
        sa.Column("signature", sa.Text(), server_default="", nullable=False),
        sa.Column("status", sa.String(length=16), server_default="unsigned", nullable=False),
        sa.Column("created_by", sa.String(length=128), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_audit_chain_checkpoints_checkpoint_id"), "audit_chain_checkpoints", ["checkpoint_id"], unique=True)
    op.create_index(op.f("ix_audit_chain_checkpoints_through_seq"), "audit_chain_checkpoints", ["through_seq"])


def downgrade() -> None:
    op.drop_index(op.f("ix_audit_chain_checkpoints_through_seq"), table_name="audit_chain_checkpoints")
    op.drop_index(op.f("ix_audit_chain_checkpoints_checkpoint_id"), table_name="audit_chain_checkpoints")
    op.drop_table("audit_chain_checkpoints")
    op.drop_index("ix_audit_chain_entries_ref", table_name="audit_chain_entries")
    op.drop_index(op.f("ix_audit_chain_entries_run_id"), table_name="audit_chain_entries")
    op.drop_index(op.f("ix_audit_chain_entries_tenant_id"), table_name="audit_chain_entries")
    op.drop_index(op.f("ix_audit_chain_entries_entry_type"), table_name="audit_chain_entries")
    op.drop_index(op.f("ix_audit_chain_entries_entry_id"), table_name="audit_chain_entries")
    op.drop_table("audit_chain_entries")
    op.drop_index("ix_change_requests_target_status", table_name="change_requests")
    op.drop_index(op.f("ix_change_requests_status"), table_name="change_requests")
    op.drop_index(op.f("ix_change_requests_tenant_id"), table_name="change_requests")
    op.drop_index(op.f("ix_change_requests_change_id"), table_name="change_requests")
    op.drop_table("change_requests")
    op.drop_index(op.f("ix_dashboard_sessions_user_id"), table_name="dashboard_sessions")
    op.drop_index(op.f("ix_dashboard_sessions_session_hash"), table_name="dashboard_sessions")
    op.drop_index(op.f("ix_dashboard_sessions_session_id"), table_name="dashboard_sessions")
    op.drop_table("dashboard_sessions")
    op.drop_index("ux_dashboard_users_external", table_name="dashboard_users")
    op.drop_index(op.f("ix_dashboard_users_tenant_id"), table_name="dashboard_users")
    op.drop_index(op.f("ix_dashboard_users_username"), table_name="dashboard_users")
    op.drop_index(op.f("ix_dashboard_users_user_id"), table_name="dashboard_users")
    op.drop_table("dashboard_users")
