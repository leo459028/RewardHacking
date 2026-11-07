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
    # v1.5.8 evidence-based evaluation fields
    tenant_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False, default="default")
    evaluator_version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    evaluation_source: Mapped[str] = mapped_column(String(32), index=True, nullable=False, default="executor")
    assurance_level: Mapped[str] = mapped_column(String(32), nullable=False, default="simulated")
    findings: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    review_priority: Mapped[str] = mapped_column(String(8), nullable=False, default="")
    outcome_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False, default="")
    created_at = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
