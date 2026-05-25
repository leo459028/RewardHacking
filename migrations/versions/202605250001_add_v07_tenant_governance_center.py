"""add v0.7 tenant governance center

Revision ID: 202605250001
Revises: 202605240002
Create Date: 2026-05-25 00:01:00
"""
from alembic import op
import sqlalchemy as sa

revision = "202605250001"
down_revision = "202605240002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("request_limit_per_minute", sa.Integer(), nullable=False),
        sa.Column("high_risk_limit_per_hour", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_tenants_tenant_id"), "tenants", ["tenant_id"], unique=True)
    op.create_index(op.f("ix_tenants_status"), "tenants", ["status"], unique=False)

    op.create_table(
        "tenant_api_keys",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("key_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("key_prefix", sa.String(length=16), nullable=False),
        sa.Column("key_hash", sa.String(length=128), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_tenant_api_keys_key_id"), "tenant_api_keys", ["key_id"], unique=True)
    op.create_index(op.f("ix_tenant_api_keys_tenant_id"), "tenant_api_keys", ["tenant_id"], unique=False)
    op.create_index(op.f("ix_tenant_api_keys_key_prefix"), "tenant_api_keys", ["key_prefix"], unique=False)
    op.create_index(op.f("ix_tenant_api_keys_key_hash"), "tenant_api_keys", ["key_hash"], unique=True)
    op.create_index(op.f("ix_tenant_api_keys_role"), "tenant_api_keys", ["role"], unique=False)
    op.create_index(op.f("ix_tenant_api_keys_status"), "tenant_api_keys", ["status"], unique=False)

    op.execute(
        """
        INSERT INTO tenants (
            tenant_id,
            display_name,
            status,
            request_limit_per_minute,
            high_risk_limit_per_hour
        )
        SELECT DISTINCT tenant_id, tenant_id, 'active', 60, 10
        FROM agent_runs
        WHERE tenant_id IS NOT NULL
        ON CONFLICT (tenant_id) DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO tenants (
            tenant_id,
            display_name,
            status,
            request_limit_per_minute,
            high_risk_limit_per_hour
        )
        VALUES ('default', 'Default Tenant', 'active', 60, 10)
        ON CONFLICT (tenant_id) DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_tenant_api_keys_status"), table_name="tenant_api_keys")
    op.drop_index(op.f("ix_tenant_api_keys_role"), table_name="tenant_api_keys")
    op.drop_index(op.f("ix_tenant_api_keys_key_hash"), table_name="tenant_api_keys")
    op.drop_index(op.f("ix_tenant_api_keys_key_prefix"), table_name="tenant_api_keys")
    op.drop_index(op.f("ix_tenant_api_keys_tenant_id"), table_name="tenant_api_keys")
    op.drop_index(op.f("ix_tenant_api_keys_key_id"), table_name="tenant_api_keys")
    op.drop_table("tenant_api_keys")
    op.drop_index(op.f("ix_tenants_status"), table_name="tenants")
    op.drop_index(op.f("ix_tenants_tenant_id"), table_name="tenants")
    op.drop_table("tenants")
