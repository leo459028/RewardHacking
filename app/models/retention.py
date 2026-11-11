"""v1.6.0 audit retention: legal holds, purge plans / receipts and hash-only tombstones.

- ``LegalHold`` (``lh_``): protects a tenant or a single run from retention purges (D15).
- ``RetentionPurge`` (``purge_``): a read-only plan (D11) whose row also carries the apply receipt (D13).
- ``RetentionTombstone`` (``tomb_``): what remains of a purged run. Hashes and counts only, never content.
  ``chain_version`` / ``prev_hash`` / ``entry_hash`` are reserved for a later tamper-evident hash chain.
"""

from sqlalchemy import JSON, DateTime, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class LegalHold(Base):
    __tablename__ = "legal_holds"
    __table_args__ = (Index("ix_legal_holds_scope_status", "scope_type", "scope_id", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    hold_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False)  # tenant | run
    scope_id: Mapped[str] = mapped_column(String(128), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False, default="default")
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")  # active | released
    created_by: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    created_at = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    released_by: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    released_at = mapped_column(DateTime(timezone=True), nullable=True)
    release_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")


class RetentionPurge(Base):
    __tablename__ = "retention_purges"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    purge_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    # planned | applying | applied | partial | expired
    status: Mapped[str] = mapped_column(String(16), index=True, nullable=False, default="planned")
    plan_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    cutoff_at = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at = mapped_column(DateTime(timezone=True), nullable=False)
    settings_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    candidate_run_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    excluded_summary: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    estimated_counts: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    scan_summary: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_by: Mapped[str] = mapped_column(String(128), index=True, nullable=False, default="")
    applied_by: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    applied_at = mapped_column(DateTime(timezone=True), nullable=True)
    applied_counts: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    skipped_at_apply: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    failures: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    receipt_digest: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class RetentionTombstone(Base):
    __tablename__ = "retention_tombstones"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tombstone_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False, default="default")
    purge_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    purged_at = mapped_column(DateTime(timezone=True), nullable=False)
    run_created_at = mapped_column(DateTime(timezone=True), nullable=True)
    final_status: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    record_counts: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    content_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    chain_version: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    prev_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entry_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
