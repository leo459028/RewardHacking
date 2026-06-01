from sqlalchemy import JSON, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class RunCheckpoint(Base):
    __tablename__ = "run_checkpoints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    checkpoint_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False, default="default")
    trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False, default="")
    checkpoint_type: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    resume_cursor: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    state_payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False, default="system")
    created_at = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ReplayAttempt(Base):
    __tablename__ = "replay_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    replay_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    source_run_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False, default="default")
    trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False, default="")
    mode: Mapped[str] = mapped_column(String(32), nullable=False, default="dry_run")
    status: Mapped[str] = mapped_column(String(32), index=True, nullable=False, default="completed")
    result_payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    limitations: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False, default="system")
    created_at = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
