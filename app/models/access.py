"""v1.6.1 Enterprise Access: Dashboard users, server-side sessions and dual-control change requests.

- ``DashboardUser`` (``usr_``): a personal Dashboard account (plan D11). ``password_hash`` uses the self-describing
  ``scrypt$n$r$p$salt$hash`` format (D12); ``tenant_id`` is NULL for platform users (D22); ``external_subject`` is
  reserved for a later OIDC provider (D16).
- ``DashboardSession`` (``ses_``): one sign-in (D15). Only the SHA-256 of the cookie token is stored, never the token.
- ``ChangeRequest`` (``chg_``): a pending second approval for a high-risk action (D37). The table is created in P1;
  the service that uses it arrives in P6.
"""

from sqlalchemy import JSON, Boolean, DateTime, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class DashboardUser(Base):
    __tablename__ = "dashboard_users"
    __table_args__ = (Index("ux_dashboard_users_external", "auth_source", "external_subject", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    tenant_id: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")  # active | disabled
    auth_source: Mapped[str] = mapped_column(String(32), nullable=False, default="local")
    external_subject: Mapped[str | None] = mapped_column(String(255), nullable=True)
    failed_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until = mapped_column(DateTime(timezone=True), nullable=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    password_changed_at = mapped_column(DateTime(timezone=True), nullable=True)
    last_login_at = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    created_at = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class DashboardSession(Base):
    __tablename__ = "dashboard_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    session_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    auth_method: Mapped[str] = mapped_column(String(32), nullable=False, default="local_password")
    csrf_secret: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at = mapped_column(DateTime(timezone=True), nullable=False)
    reauth_at = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_reason: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    user_agent_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")


class ChangeRequest(Base):
    __tablename__ = "change_requests"
    __table_args__ = (Index("ix_change_requests_target_status", "action", "target_ref", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    change_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    tenant_id: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    # pending | approved | rejected | executed | failed | expired | cancelled
    status: Mapped[str] = mapped_column(String(16), index=True, nullable=False, default="pending")
    requested_by: Mapped[str] = mapped_column(String(128), nullable=False)
    requested_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    requested_at = mapped_column(DateTime(timezone=True), nullable=False)
    decided_by: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    decision_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    decided_at = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at = mapped_column(DateTime(timezone=True), nullable=False)
    executed_at = mapped_column(DateTime(timezone=True), nullable=True)
    result: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    error_code: Mapped[str] = mapped_column(String(64), nullable=False, default="")
