"""v1.6.1 tamper-evident audit chain: a separate hash ledger and its signed checkpoints (plan D28-D33).

- ``AuditChainEntry`` (``ace_``): one hash-linked entry per governance event, review decision, approval decision,
  tombstone or checkpoint. Hashes and identifiers only, never content. Retention purges never delete this table.
  ``seq`` is assigned by the writer (last ``seq`` + 1, under a lock) because it is part of ``entry_hash``.
- ``AuditChainCheckpoint`` (``acp_``): a periodic signed (Ed25519) or ``unsigned`` statement of the chain head.

The tables are created in P1; the service that writes them arrives in P5.
"""

from sqlalchemy import BigInteger, DateTime, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AuditChainEntry(Base):
    __tablename__ = "audit_chain_entries"
    __table_args__ = (Index("ix_audit_chain_entries_ref", "ref_table", "ref_id"),)

    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    entry_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    entry_type: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    ref_table: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    ref_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    tenant_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False, default="")
    run_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False, default="")
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    entry_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    chain_version: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at = mapped_column(DateTime(timezone=True), nullable=False)


class AuditChainCheckpoint(Base):
    __tablename__ = "audit_chain_checkpoints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    checkpoint_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    through_seq: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    head_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    entry_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    period_start = mapped_column(DateTime(timezone=True), nullable=True)
    period_end = mapped_column(DateTime(timezone=True), nullable=False)
    prev_checkpoint_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    checkpoint_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    algorithm: Mapped[str] = mapped_column(String(16), nullable=False, default="ed25519")
    key_id: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    signature: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="unsigned")  # signed | unsigned
    created_by: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    created_at = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
