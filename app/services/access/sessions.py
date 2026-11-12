"""Server-side Dashboard sessions (v1.6.1 plan D15).

The cookie ``rh_session`` carries a 256-bit random token; the database stores only its SHA-256 (``session_hash``).
A session ends at the idle timeout (``DASHBOARD_SESSION_IDLE_MINUTES``), the absolute timeout
(``DASHBOARD_SESSION_MAX_HOURS``), sign-out, or when the user is disabled, changes password or changes role.
``last_seen_at`` is written at most every 60 seconds. Functions here never commit; the caller commits.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.access import DashboardSession, DashboardUser
from app.services.access.errors import aware, utcnow

COOKIE_NAME = "rh_session"
COOKIE_PATH = "/dashboard"
TOKEN_BYTES = 32  # 256 bits
LAST_SEEN_WRITE_SECONDS = 60


def new_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def user_agent_hash(user_agent: str | None) -> str:
    return hashlib.sha256((user_agent or "").encode("utf-8")).hexdigest() if user_agent else ""


@dataclass
class SessionLookup:
    session_row: DashboardSession
    user: DashboardUser
    touched: bool  # last_seen_at was updated; the caller should commit


def _expired_reason(row: DashboardSession, settings: Settings, now: datetime) -> str | None:
    if row.revoked_at is not None:
        return row.revoked_reason or "revoked"
    if aware(row.expires_at) <= now:
        return "absolute_timeout"
    if aware(row.last_seen_at) + timedelta(minutes=settings.dashboard_session_idle_minutes) <= now:
        return "idle_timeout"
    return None


async def create_session(
    session: AsyncSession,
    settings: Settings,
    user: DashboardUser,
    *,
    auth_method: str,
    user_agent: str | None = None,
    now: datetime | None = None,
) -> tuple[str, DashboardSession]:
    """Always a new session (D15: no session fixation). Returns ``(token, row)``; only the row is stored."""
    now = now or utcnow()
    token = new_token()
    row = DashboardSession(
        session_id=f"ses_{uuid.uuid4().hex}",
        session_hash=token_hash(token),
        user_id=user.user_id,
        auth_method=auth_method,
        csrf_secret=secrets.token_hex(32),
        created_at=now,
        last_seen_at=now,
        expires_at=now + timedelta(hours=settings.dashboard_session_max_hours),
        reauth_at=now,  # D26: a fresh sign-in counts as a recent re-authentication
        user_agent_hash=user_agent_hash(user_agent),
    )
    session.add(row)
    await session.flush()
    return token, row


async def resolve_session(
    session: AsyncSession, settings: Settings, token: str | None, *, now: datetime | None = None
) -> SessionLookup | None:
    """The live session and active user for a cookie token, or ``None`` (unknown, expired, revoked, user disabled)."""
    if not token:
        return None
    now = now or utcnow()
    row = await session.scalar(select(DashboardSession).where(DashboardSession.session_hash == token_hash(token)))
    if row is None:
        return None
    reason = _expired_reason(row, settings, now)
    if reason is not None:
        if row.revoked_at is None:
            row.revoked_at = now
            row.revoked_reason = reason
            await session.flush()
        return None
    user = await session.scalar(select(DashboardUser).where(DashboardUser.user_id == row.user_id))
    if user is None or user.status != "active":
        return None
    touched = False
    if aware(row.last_seen_at) + timedelta(seconds=LAST_SEEN_WRITE_SECONDS) <= now:
        row.last_seen_at = now
        touched = True
        await session.flush()
    return SessionLookup(session_row=row, user=user, touched=touched)


async def revoke_session(session: AsyncSession, row: DashboardSession, *, reason: str, now: datetime | None = None) -> None:
    if row.revoked_at is None:
        row.revoked_at = now or utcnow()
        row.revoked_reason = reason[:32]
        await session.flush()


async def revoke_user_sessions(
    session: AsyncSession, user_id: str, *, reason: str, now: datetime | None = None, except_session_id: str | None = None
) -> int:
    """Revoke every live session of a user (sign-out everywhere); returns how many were revoked."""
    stmt = (
        update(DashboardSession)
        .where(DashboardSession.user_id == user_id, DashboardSession.revoked_at.is_(None))
        .values(revoked_at=now or utcnow(), revoked_reason=reason[:32])
    )
    if except_session_id:
        stmt = stmt.where(DashboardSession.session_id != except_session_id)
    result = await session.execute(stmt.execution_options(synchronize_session="fetch"))
    return int(result.rowcount or 0)


async def mark_reauthenticated(session: AsyncSession, row: DashboardSession, *, now: datetime | None = None) -> None:
    row.reauth_at = now or utcnow()
    await session.flush()


def reauth_is_fresh(reauth_at: datetime | None, settings: Settings, *, now: datetime | None = None) -> bool:
    """D26: was the last (re-)authentication within ``DASHBOARD_REAUTH_MAX_AGE_SECONDS``?"""
    if reauth_at is None:
        return False
    now = now or utcnow()
    return aware(reauth_at) + timedelta(seconds=settings.dashboard_reauth_max_age_seconds) > now


async def list_user_sessions(session: AsyncSession, user_id: str, *, live_only: bool = True) -> list[DashboardSession]:
    stmt = select(DashboardSession).where(DashboardSession.user_id == user_id)
    if live_only:
        stmt = stmt.where(DashboardSession.revoked_at.is_(None))
    return list((await session.scalars(stmt.order_by(DashboardSession.created_at.desc()))).all())
