from sqlalchemy import JSON, Boolean, DateTime, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class PricingCatalog(Base):
    __tablename__ = "pricing_catalog"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pricing_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    resource_type: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), index=True, nullable=False, default="")
    price_unit: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    unit_price: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    effective_from = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    effective_to = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    metadata_payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class UsageEvent(Base):
    __tablename__ = "usage_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    usage_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False, default="default")
    user_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False, default="")
    run_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False, default="")
    trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False, default="")
    workflow_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False, default="")
    step_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False, default="")
    checkpoint_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False, default="")
    replay_attempt_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False, default="")
    provider: Mapped[str] = mapped_column(String(64), index=True, nullable=False, default="internal")
    model_name: Mapped[str] = mapped_column(String(128), index=True, nullable=False, default="")
    resource_type: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    usage_unit: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    quantity: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    phase: Mapped[str] = mapped_column(String(32), index=True, nullable=False, default="estimated")
    metadata_payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CostEvent(Base):
    __tablename__ = "cost_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cost_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    usage_event_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    pricing_catalog_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False, default="")
    tenant_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False, default="default")
    run_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False, default="")
    trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False, default="")
    workflow_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False, default="")
    resource_type: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), index=True, nullable=False, default="")
    estimated_cost: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False, default=0)
    actual_cost: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    allocation_tags: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    calculation_method: Mapped[str] = mapped_column(String(32), index=True, nullable=False, default="estimated")
    metadata_payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class BudgetPolicy(Base):
    __tablename__ = "budget_policies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    budget_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False, default="default")
    scope: Mapped[str] = mapped_column(String(32), index=True, nullable=False, default="tenant")
    policy_name: Mapped[str] = mapped_column(String(128), nullable=False)
    period: Mapped[str] = mapped_column(String(32), index=True, nullable=False, default="monthly")
    limit_amount: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    resource_type: Mapped[str] = mapped_column(String(64), index=True, nullable=False, default="all")
    actions: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    metadata_payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class BudgetViolation(Base):
    __tablename__ = "budget_violations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    violation_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    budget_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False, default="")
    tenant_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False, default="default")
    user_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False, default="")
    run_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False, default="")
    workflow_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False, default="")
    model_name: Mapped[str] = mapped_column(String(128), index=True, nullable=False, default="")
    violation_type: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    severity: Mapped[str] = mapped_column(String(32), index=True, nullable=False, default="warning")
    actual_amount: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False)
    limit_amount: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    decision: Mapped[str] = mapped_column(String(32), index=True, nullable=False, default="warn")
    details: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
