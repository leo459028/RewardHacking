"""add v1.4 checkpoints and replay attempts

Revision ID: 202606010002
Revises: 202606010001
Create Date: 2026-06-01 16:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "202606010002"
down_revision = "202606010001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "run_checkpoints",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("checkpoint_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("trace_id", sa.String(length=128), nullable=False),
        sa.Column("checkpoint_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("resume_cursor", sa.String(length=64), nullable=False),
        sa.Column("state_payload", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_run_checkpoints_checkpoint_id"), "run_checkpoints", ["checkpoint_id"], unique=True)
    op.create_index(op.f("ix_run_checkpoints_run_id"), "run_checkpoints", ["run_id"])
    op.create_index(op.f("ix_run_checkpoints_tenant_id"), "run_checkpoints", ["tenant_id"])
    op.create_index(op.f("ix_run_checkpoints_trace_id"), "run_checkpoints", ["trace_id"])
    op.create_index(op.f("ix_run_checkpoints_checkpoint_type"), "run_checkpoints", ["checkpoint_type"])
    op.create_index(op.f("ix_run_checkpoints_status"), "run_checkpoints", ["status"])

    op.create_table(
        "replay_attempts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("replay_id", sa.String(length=64), nullable=False),
        sa.Column("source_run_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("trace_id", sa.String(length=128), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("result_payload", sa.JSON(), nullable=False),
        sa.Column("limitations", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_replay_attempts_replay_id"), "replay_attempts", ["replay_id"], unique=True)
    op.create_index(op.f("ix_replay_attempts_source_run_id"), "replay_attempts", ["source_run_id"])
    op.create_index(op.f("ix_replay_attempts_tenant_id"), "replay_attempts", ["tenant_id"])
    op.create_index(op.f("ix_replay_attempts_trace_id"), "replay_attempts", ["trace_id"])
    op.create_index(op.f("ix_replay_attempts_status"), "replay_attempts", ["status"])


def downgrade() -> None:
    op.drop_index(op.f("ix_replay_attempts_status"), table_name="replay_attempts")
    op.drop_index(op.f("ix_replay_attempts_trace_id"), table_name="replay_attempts")
    op.drop_index(op.f("ix_replay_attempts_tenant_id"), table_name="replay_attempts")
    op.drop_index(op.f("ix_replay_attempts_source_run_id"), table_name="replay_attempts")
    op.drop_index(op.f("ix_replay_attempts_replay_id"), table_name="replay_attempts")
    op.drop_table("replay_attempts")

    op.drop_index(op.f("ix_run_checkpoints_status"), table_name="run_checkpoints")
    op.drop_index(op.f("ix_run_checkpoints_checkpoint_type"), table_name="run_checkpoints")
    op.drop_index(op.f("ix_run_checkpoints_trace_id"), table_name="run_checkpoints")
    op.drop_index(op.f("ix_run_checkpoints_tenant_id"), table_name="run_checkpoints")
    op.drop_index(op.f("ix_run_checkpoints_run_id"), table_name="run_checkpoints")
    op.drop_index(op.f("ix_run_checkpoints_checkpoint_id"), table_name="run_checkpoints")
    op.drop_table("run_checkpoints")
