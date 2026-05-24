from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ApprovalEvent(Base):
    __tablename__ = "approval_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    reviewer: Mapped[str] = mapped_column(String(128), index=True, nullable=False, default="")
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    expires_at = mapped_column(DateTime(timezone=True), nullable=True)
    expired: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
