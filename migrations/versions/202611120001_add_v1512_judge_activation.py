"""add v1.5.12 judge profile configs (Dashboard source) and judge profile activation states

Revision ID: 202611120001
Revises: 202611100001
Create Date: 2026-11-12 09:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "202611120001"
down_revision = "202611100001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Dashboard-managed judge profiles: non-secret fields only (validated by parse_profile), optimistic locking.
    op.create_table(
        "judge_profile_configs",
        sa.Column("name", sa.String(length=48), nullable=False),
        sa.Column("config", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_by", sa.String(length=128), server_default="", nullable=False),
        sa.Column("updated_by", sa.String(length=128), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("name"),
    )
    # Probe results and activation per profile (any source). Fingerprints only; never a key or a full key hash.
    op.create_table(
        "judge_profile_states",
        sa.Column("profile_name", sa.String(length=48), nullable=False),
        sa.Column("probe_status", sa.String(length=32), server_default="", nullable=False),
        sa.Column("probe_error", sa.String(length=200), server_default="", nullable=False),
        sa.Column("probe_fingerprint", sa.String(length=64), server_default="", nullable=False),
        sa.Column("probed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("probe_result", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("activation_fingerprint", sa.String(length=64), server_default="", nullable=False),
        sa.Column("activated_by", sa.String(length=128), server_default="", nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("profile_name"),
    )
    op.create_index(op.f("ix_judge_profile_states_active"), "judge_profile_states", ["active"])


def downgrade() -> None:
    op.drop_index(op.f("ix_judge_profile_states_active"), table_name="judge_profile_states")
    op.drop_table("judge_profile_states")
    op.drop_table("judge_profile_configs")
