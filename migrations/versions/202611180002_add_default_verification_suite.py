"""add the tenant's default verification suite (v1.6.2 D24)

Revision ID: 202611180002
Revises: 202611180001
Create Date: 2026-11-18 20:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "202611180002"
down_revision = "202611180001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenant_evaluation_policies",
        sa.Column("default_verification_suite_id", sa.String(length=64), server_default="", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("tenant_evaluation_policies", "default_verification_suite_id")
