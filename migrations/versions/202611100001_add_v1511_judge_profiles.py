"""add v1.5.11 judge profile column and tenant external-judge consent

Revision ID: 202611100001
Revises: 202611090001
Create Date: 2026-11-10 09:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "202611100001"
down_revision = "202611090001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Server defaults keep existing v1.5.10 rows valid without backfill.
    op.add_column("judge_assessments", sa.Column("profile", sa.String(length=48), server_default="", nullable=False))
    op.create_index(op.f("ix_judge_assessments_profile"), "judge_assessments", ["profile"])
    op.add_column(
        "tenant_evaluation_policies",
        sa.Column("allow_external_judge", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column(
        "tenant_evaluation_policies",
        sa.Column("external_judge_profiles", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("tenant_evaluation_policies", "external_judge_profiles")
    op.drop_column("tenant_evaluation_policies", "allow_external_judge")
    op.drop_index(op.f("ix_judge_assessments_profile"), table_name="judge_assessments")
    op.drop_column("judge_assessments", "profile")
