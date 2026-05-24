from sqlalchemy import JSON, DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class EvaluationResultRecord(Base):
    __tablename__ = "evaluation_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    task_type: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    verdict: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    final_score: Mapped[float] = mapped_column(Float, nullable=False)
    rubric: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    judge_signal: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    explanation: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
