"""Dashboard user management (v1.6.1 plan D11, D13, D14, D17, D18).

Rules enforced here (so the Dashboard and the CLI behave the same):

- usernames are lower-case and unique; passwords follow ``passwords.password_problems()``;
- role and tenant fit together (``permissions.tenant_rule_problem()``);
- the last active ``admin`` can be neither disabled nor demoted, and nobody changes their own role or disables
  themselves;
- disabling a user, changing a role or changing a password revokes all of that user's sessions;
- every change writes an ``access.*`` governance event (tenant ``system``) without passwords, hashes or tokens.

Functions never commit: the event and the row change are committed together by the caller.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.access import DashboardUser
from app.services.access import passwords, permissions, sessions
from app.services.access.errors import AccessError, aware, utcnow
from app.services.governance_audit import add_governance_event

SYSTEM_TENANT = "system"
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._@-]{1,63}$")
STATUSES = ("active", "disabled")


def normalize_username(raw: str) -> str:
    username = (raw or "").strip().lower()
    if not USERNAME_RE.fullmatch(username):
        raise AccessError(
            "invalid_username", "Usernames are 2-64 characters: a-z, 0-9, '.', '_', '@', '-' (starting with a letter or digit)."
        )
    return username


def _normalize_tenant(tenant_id: str | None) -> str | None:
    value = (tenant_id or "").strip()
    return value or None


def _check_role(role: str, tenant_id: str | None) -> None:
    problem = permissions.tenant_rule_problem(role, tenant_id)
    if problem == "invalid_role":
        raise AccessError(problem, f"Role must be one of: {', '.join(permissions.ROLES)}.")
    if problem == "tenant_required_for_role":
        raise AccessError(problem, "A tenant_admin must be bound to a tenant.")
    if problem == "role_is_platform_only":
        raise AccessError(problem, "An admin is a platform role and cannot be bound to a tenant.")


def check_password(password: str, *, username: str, settings: Settings) -> None:
    problems = passwords.password_problems(password, username=username, min_length=settings.dashboard_password_min_length)
    if problems:
        raise AccessError(
            problems[0],
            f"Password rejected ({', '.join(problems)}). Use at least {settings.dashboard_password_min_length} characters, "
            "not the username and not a common password.",
            details={"problems": problems},
        )


def user_view(user: DashboardUser) -> dict[str, Any]:
    """A safe summary (never the password hash)."""
    return {
        "user_id": user.user_id,
        "username": user.username,
        "display_name": user.display_name,
        "role": user.role,
        "tenant_id": user.tenant_id,
        "status": user.status,
        "auth_source": user.auth_source,
        "must_change_password": bool(user.must_change_password),
        "locked_until": aware(user.locked_until).isoformat() if user.locked_until else None,
        "failed_attempts": int(user.failed_attempts or 0),
        "last_login_at": aware(user.last_login_at).isoformat() if user.last_login_at else None,
        "password_changed_at": aware(user.password_changed_at).isoformat() if user.password_changed_at else None,
        "created_by": user.created_by,
        "created_at": aware(user.created_at).isoformat() if user.created_at else None,
    }


def access_event(
    session: AsyncSession, event_type: str, *, actor: str, decision: str = "allow", reason: str = "", details: dict[str, Any] | None = None
) -> None:
    """D18: an ``access.*`` governance event in the caller's transaction (tenant ``system``)."""
    add_governance_event(
        session,
        tenant_id=SYSTEM_TENANT,
        user_id=actor,
        event_type=event_type,
        decision=decision,
        reason=reason,
        details=details or {},
    )


async def get_user(session: AsyncSession, user_id: str) -> DashboardUser | None:
    return await session.scalar(select(DashboardUser).where(DashboardUser.user_id == user_id))


async def get_user_by_username(session: AsyncSession, username: str) -> DashboardUser | None:
    return await session.scalar(select(DashboardUser).where(DashboardUser.username == (username or "").strip().lower()))


async def list_users(session: AsyncSession) -> list[DashboardUser]:
    return list((await session.scalars(select(DashboardUser).order_by(DashboardUser.username))).all())


async def count_active_admins(session: AsyncSession) -> int:
    return int(
        await session.scalar(
            select(func.count()).select_from(DashboardUser).where(DashboardUser.role == permissions.ADMIN, DashboardUser.status == "active")
        )
        or 0
    )


async def _require(session: AsyncSession, user_id: str) -> DashboardUser:
    user = await get_user(session, user_id)
    if user is None:
        raise AccessError("user_not_found", "User not found.", status_code=404)
    return user


async def _protect_last_admin(session: AsyncSession, user: DashboardUser) -> None:
    if user.role == permissions.ADMIN and user.status == "active" and await count_active_admins(session) <= 1:
        raise AccessError("last_admin_protected", "The last active admin cannot be disabled or demoted.", status_code=409)


async def create_user(
    session: AsyncSession,
    settings: Settings,
    *,
    username: str,
    password: str,
    role: str,
    tenant_id: str | None = None,
    display_name: str = "",
    actor: str,
    must_change_password: bool = True,
) -> DashboardUser:
    """D13 / D14: new accounts must change their password at first sign-in unless told otherwise."""
    username = normalize_username(username)
    tenant_id = _normalize_tenant(tenant_id)
    _check_role(role, tenant_id)
    check_password(password, username=username, settings=settings)
    if await get_user_by_username(session, username) is not None:
        raise AccessError("username_taken", "That username is already in use.", status_code=409)
    now = utcnow()
    user = DashboardUser(
        user_id=f"usr_{uuid.uuid4().hex[:24]}",
        username=username,
        display_name=(display_name or "").strip()[:128],
        password_hash=await passwords.hash_password_async(password, passwords.params_from_settings(settings)),
        role=role,
        tenant_id=tenant_id,
        status="active",
        auth_source="local",
        failed_attempts=0,
        must_change_password=must_change_password,
        password_changed_at=now,
        created_by=actor,
        created_at=now,
        updated_at=now,
    )
    session.add(user)
    await session.flush()
    access_event(
        session,
        "access.user_created",
        actor=actor,
        reason=f"Created Dashboard user {username} ({role}).",
        details={"target_user_id": user.user_id, "username": username, "role": role, "tenant_id": tenant_id},
    )
    return user


async def change_role(
    session: AsyncSession, *, user_id: str, role: str, tenant_id: str | None, actor: str, actor_user_id: str | None = None
) -> DashboardUser:
    user = await _require(session, user_id)
    tenant_id = _normalize_tenant(tenant_id)
    _check_role(role, tenant_id)
    if actor_user_id and actor_user_id == user.user_id:
        raise AccessError("cannot_change_own_role", "You cannot change your own role.", status_code=403)
    if (role, tenant_id) == (user.role, user.tenant_id):
        return user
    if role != permissions.ADMIN:
        await _protect_last_admin(session, user)
    before = {"role": user.role, "tenant_id": user.tenant_id}
    user.role, user.tenant_id = role, tenant_id
    user.updated_at = utcnow()
    revoked = await sessions.revoke_user_sessions(session, user.user_id, reason="role_changed")
    access_event(
        session,
        "access.role_changed",
        actor=actor,
        reason=f"Changed role of {user.username} from {before['role']} to {role}.",
        details={"target_user_id": user.user_id, "username": user.username, "before": before,
                 "after": {"role": role, "tenant_id": tenant_id}, "sessions_revoked": revoked},
    )
    await session.flush()
    return user


async def update_profile(session: AsyncSession, *, user_id: str, display_name: str, actor: str) -> DashboardUser:
    user = await _require(session, user_id)
    user.display_name = (display_name or "").strip()[:128]
    user.updated_at = utcnow()
    access_event(session, "access.user_updated", actor=actor, reason=f"Updated profile of {user.username}.",
                 details={"target_user_id": user.user_id, "username": user.username, "fields": ["display_name"]})
    await session.flush()
    return user


async def set_status(
    session: AsyncSession, *, user_id: str, status: str, actor: str, actor_user_id: str | None = None
) -> DashboardUser:
    if status not in STATUSES:
        raise AccessError("invalid_status", "Status must be 'active' or 'disabled'.")
    user = await _require(session, user_id)
    if status == user.status:
        return user
    if status == "disabled":
        if actor_user_id and actor_user_id == user.user_id:
            raise AccessError("cannot_disable_self", "You cannot disable your own account.", status_code=403)
        await _protect_last_admin(session, user)
    user.status = status
    user.updated_at = utcnow()
    details: dict[str, Any] = {"target_user_id": user.user_id, "username": user.username, "status": status}
    if status == "disabled":
        details["sessions_revoked"] = await sessions.revoke_user_sessions(session, user.user_id, reason="user_disabled")
        access_event(session, "access.user_disabled", actor=actor, reason=f"Disabled {user.username}.", details=details)
    else:
        user.failed_attempts, user.locked_until = 0, None
        access_event(session, "access.user_updated", actor=actor, reason=f"Enabled {user.username}.", details=details)
    await session.flush()
    return user


async def set_password(
    session: AsyncSession,
    settings: Settings,
    *,
    user_id: str,
    new_password: str,
    actor: str,
    must_change_password: bool,
    keep_session_id: str | None = None,
) -> DashboardUser:
    """Change (own) or reset (admin, ``must_change_password=True``) a password; revokes the user's sessions (D15).

    ``keep_session_id`` is not used by the Dashboard (it re-issues a fresh session after a self-service change);
    it exists for callers that must keep one session alive.
    """
    user = await _require(session, user_id)
    check_password(new_password, username=user.username, settings=settings)
    if user.password_hash and await passwords.verify_password_async(new_password, user.password_hash):
        raise AccessError("password_unchanged", "The new password must differ from the current one.")
    now = utcnow()
    user.password_hash = await passwords.hash_password_async(new_password, passwords.params_from_settings(settings))
    user.password_changed_at = now
    user.must_change_password = must_change_password
    user.failed_attempts, user.locked_until = 0, None
    user.updated_at = now
    revoked = await sessions.revoke_user_sessions(session, user.user_id, reason="password_changed", now=now, except_session_id=keep_session_id)
    access_event(
        session,
        "access.password_changed",
        actor=actor,
        reason=f"Password {'reset' if must_change_password else 'changed'} for {user.username}.",
        details={"target_user_id": user.user_id, "username": user.username, "reset": must_change_password, "sessions_revoked": revoked},
    )
    await session.flush()
    return user


async def unlock(session: AsyncSession, *, user_id: str, actor: str) -> DashboardUser:
    user = await _require(session, user_id)
    user.failed_attempts, user.locked_until = 0, None
    user.updated_at = utcnow()
    access_event(session, "access.user_updated", actor=actor, reason=f"Unlocked {user.username}.",
                 details={"target_user_id": user.user_id, "username": user.username, "fields": ["locked_until"]})
    await session.flush()
    return user


def is_locked(user: DashboardUser, *, now: datetime | None = None) -> bool:
    return user.locked_until is not None and aware(user.locked_until) > (now or utcnow())


async def record_failed_login(session: AsyncSession, settings: Settings, user: DashboardUser, *, now: datetime | None = None) -> bool:
    """D17: count a wrong password; the ``DASHBOARD_LOGIN_MAX_FAILURES``-th one locks the account. Returns ``True``
    when this attempt locked it. Failures during a lock do not extend it. Individual failures are not written as
    governance events (only the lockout), so the event table cannot be flooded."""
    now = now or utcnow()
    if is_locked(user, now=now):
        return False
    user.failed_attempts = int(user.failed_attempts or 0) + 1
    locked = user.failed_attempts >= settings.dashboard_login_max_failures
    if locked:
        user.locked_until = now + timedelta(minutes=settings.dashboard_login_lockout_minutes)
        user.failed_attempts = 0
        access_event(
            session,
            "access.lockout",
            actor=f"user:{user.username}",
            decision="deny",
            reason=f"Locked {user.username} for {settings.dashboard_login_lockout_minutes} minutes after "
            f"{settings.dashboard_login_max_failures} failed sign-ins.",
            details={"target_user_id": user.user_id, "username": user.username,
                     "locked_until": user.locked_until.isoformat()},
        )
    await session.flush()
    return locked


async def record_successful_login(session: AsyncSession, user: DashboardUser, *, now: datetime | None = None) -> None:
    user.failed_attempts, user.locked_until = 0, None
    user.last_login_at = now or utcnow()
    await session.flush()
