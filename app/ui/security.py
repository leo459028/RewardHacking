"""Dashboard request identity (v1.6.1 plan D9, D10, D15, D16).

``dashboard_principal`` is the one dependency every Dashboard route goes through (``app.ui.routes.dashboard_auth``
is a thin wrapper kept for the existing routers):

- ``DASHBOARD_AUTH_MODE=off``: the synthetic ``LOCAL_ADMIN`` (today's behaviour; local development only).
- ``session``: the ``rh_session`` cookie must resolve to a live session of an active user. Otherwise
  ``DashboardSignInRequired`` is raised; ``app.main`` turns it into a 303 to ``/dashboard/login?next=...`` for
  GET / HEAD and a 401 JSON error for anything else (nothing is executed).
- A user with ``must_change_password`` may only reach the account page and sign out (``PasswordChangeRequired``).

P2 deliberately stops here: every signed-in user acts with ``admin`` power until P3 attaches the permission table
(``require_permission``), and CSRF / re-authentication arrive in P4.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlsplit

from fastapi import Depends, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.infra.postgres import get_session
from app.observability.metrics import DASHBOARD_ACCESS_DENIED_TOTAL
from app.services.access import sessions
from app.services.access.principal import LOCAL_ADMIN, Principal, principal_for

LOGIN_PATH = "/dashboard/login"
LOGOUT_PATH = "/dashboard/logout"
ACCOUNT_PATH = "/dashboard/account"
DEFAULT_NEXT = "/dashboard"
# Reachable while the password must be changed (D13).
PASSWORD_CHANGE_ALLOWED = frozenset({ACCOUNT_PATH, ACCOUNT_PATH + "/password", LOGOUT_PATH})


class DashboardSignInRequired(Exception):
    """No live session in ``session`` mode."""

    def __init__(self, next_path: str) -> None:
        super().__init__("Dashboard sign-in required.")
        self.next_path = next_path


class PasswordChangeRequired(Exception):
    """The signed-in user must change their password before doing anything else."""


def safe_next(value: str | None) -> str:
    """D26 / feature five: only a relative ``/dashboard`` path is accepted (no scheme, host, ``//`` or ``\\``)."""
    candidate = (value or "").strip()
    if not candidate or "\\" in candidate or any(ord(ch) < 32 for ch in candidate):
        return DEFAULT_NEXT
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return DEFAULT_NEXT
    if parts.scheme or parts.netloc or candidate.startswith("//"):
        return DEFAULT_NEXT
    if not (parts.path == "/dashboard" or parts.path.startswith("/dashboard/")):
        return DEFAULT_NEXT
    if parts.path in {LOGIN_PATH, LOGOUT_PATH} or "/../" in parts.path or parts.path.endswith("/.."):
        return DEFAULT_NEXT
    return candidate


def request_path(request: Request) -> str:
    query = request.url.query
    return request.url.path + (f"?{query}" if query else "")


def login_redirect_url(next_path: str) -> str:
    return f"{LOGIN_PATH}?next={quote(safe_next(next_path), safe='')}"


def set_session_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        sessions.COOKIE_NAME,
        token,
        max_age=int(settings.dashboard_session_max_hours) * 3600,
        path=sessions.COOKIE_PATH,
        secure=settings.dashboard_cookie_secure_effective,
        httponly=True,
        samesite="lax",
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        sessions.COOKIE_NAME,
        path=sessions.COOKIE_PATH,
        secure=settings.dashboard_cookie_secure_effective,
        httponly=True,
        samesite="lax",
    )


async def current_session(request: Request, session: AsyncSession, settings: Settings) -> sessions.SessionLookup | None:
    """The live session for this request's cookie (``None`` when absent or ended).

    Commits what the lookup flushed (a ``last_seen_at`` bump, or marking an expired session revoked)."""
    token = request.cookies.get(sessions.COOKIE_NAME)
    if not token:
        return None
    lookup = await sessions.resolve_session(session, settings, token)
    if session.in_transaction():
        await session.commit()
    return lookup


async def dashboard_principal(request: Request, session: AsyncSession = Depends(get_session)) -> Principal:
    settings = get_settings()
    if settings.dashboard_auth_mode != "session":
        request.state.principal = LOCAL_ADMIN
        return LOCAL_ADMIN
    lookup = await current_session(request, session, settings)
    if lookup is None:
        DASHBOARD_ACCESS_DENIED_TOTAL.labels(reason="sign_in_required").inc()
        raise DashboardSignInRequired(request_path(request))
    principal = principal_for(lookup.user, lookup.session_row)
    request.state.principal = principal
    request.state.dashboard_session = lookup.session_row
    if principal.must_change_password and request.url.path not in PASSWORD_CHANGE_ALLOWED:
        DASHBOARD_ACCESS_DENIED_TOTAL.labels(reason="password_change_required").inc()
        raise PasswordChangeRequired()
    return principal


def template_principal(request: Any) -> Principal | None:
    """Jinja global: the principal of the request being rendered (``None`` on pages outside the guard)."""
    state = getattr(request, "state", None)
    return getattr(state, "principal", None) if state is not None else None


def install_template_globals(templates: Any) -> None:
    templates.env.globals["dashboard_user"] = template_principal
    templates.env.globals["dashboard_auth_mode"] = lambda: get_settings().dashboard_auth_mode
