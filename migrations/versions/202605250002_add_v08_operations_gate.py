"""add v0.8 operations gate

Revision ID: 202605250002
Revises: 202605250001
Create Date: 2026-05-25 08:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "202605250002"
down_revision = "202605250001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "operations_evidence",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("evidence_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("reference_url", sa.String(length=512), nullable=False),
        sa.Column("metadata_payload", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_operations_evidence_evidence_type"), "operations_evidence", ["evidence_type"])
    op.create_index(op.f("ix_operations_evidence_status"), "operations_evidence", ["status"])
    op.create_index(op.f("ix_operations_evidence_created_by"), "operations_evidence", ["created_by"])

    op.create_table(
        "release_gate_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("gate_id", sa.String(length=64), nullable=False),
        sa.Column("release_version", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("trace_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_release_gate_runs_gate_id"), "release_gate_runs", ["gate_id"], unique=True)
    op.create_index(op.f("ix_release_gate_runs_release_version"), "release_gate_runs", ["release_version"])
    op.create_index(op.f("ix_release_gate_runs_status"), "release_gate_runs", ["status"])
    op.create_index(op.f("ix_release_gate_runs_created_by"), "release_gate_runs", ["created_by"])
    op.create_index(op.f("ix_release_gate_runs_trace_id"), "release_gate_runs", ["trace_id"])

    op.create_table(
        "release_gate_items",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("gate_id", sa.String(length=64), nullable=False),
        sa.Column("check_key", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=128), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("metadata_payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_release_gate_items_gate_id"), "release_gate_items", ["gate_id"])
    op.create_index(op.f("ix_release_gate_items_check_key"), "release_gate_items", ["check_key"])
    op.create_index(op.f("ix_release_gate_items_status"), "release_gate_items", ["status"])


def downgrade() -> None:
    op.drop_index(op.f("ix_release_gate_items_status"), table_name="release_gate_items")
    op.drop_index(op.f("ix_release_gate_items_check_key"), table_name="release_gate_items")
    op.drop_index(op.f("ix_release_gate_items_gate_id"), table_name="release_gate_items")
    op.drop_table("release_gate_items")
    op.drop_index(op.f("ix_release_gate_runs_trace_id"), table_name="release_gate_runs")
    op.drop_index(op.f("ix_release_gate_runs_created_by"), table_name="release_gate_runs")
    op.drop_index(op.f("ix_release_gate_runs_status"), table_name="release_gate_runs")
    op.drop_index(op.f("ix_release_gate_runs_release_version"), table_name="release_gate_runs")
    op.drop_index(op.f("ix_release_gate_runs_gate_id"), table_name="release_gate_runs")
    op.drop_table("release_gate_runs")
    op.drop_index(op.f("ix_operations_evidence_created_by"), table_name="operations_evidence")
    op.drop_index(op.f("ix_operations_evidence_status"), table_name="operations_evidence")
    op.drop_index(op.f("ix_operations_evidence_evidence_type"), table_name="operations_evidence")
    op.drop_table("operations_evidence")
