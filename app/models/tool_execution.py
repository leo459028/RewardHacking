from sqlalchemy import JSON, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ToolExecution(Base):
    __tablename__ = "tool_executions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    tool_name: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    sandbox_policy: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    artifact_refs: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_at = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
