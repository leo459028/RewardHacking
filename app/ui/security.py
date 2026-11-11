"""Dashboard request identity (v1.6.1 plan D9, D10, D15, D16).

``dashboard_principal`` is the one dependency every Dashboard route goes through (``app.ui.routes.dashboard_auth``
is a thin wrapper kept for the existing routers):

- ``DASHBOARD_AUTH_MODE=off``: the synthetic ``LOCAL_ADMIN`` (today's behaviour; local development only).
- ``session``: the ``rh_session`` cookie must resolve to a live session of an active user. Otherwise
  ``DashboardSignInRequired`` is raised; ``app.main`` turns it into a 303 to ``/dashboard/login?next=...`` for
  GET / HEAD and a 401 JSON error for anything else (nothing is executed).
- A user with ``must_change_password`` may only reach the account page and sign out (``PasswordChangeRequired``).

P3 (plan D19-D23) adds authorization on top:

- ``require_permission("<name>", view=...)`` is declared on every Dashboard route (``public_route()`` on the sign-in
  pages). A route without a declaration is refused at run time (``permission_undeclared``) and by the contract test.
- ``view`` is the D22 view name: tenant-bound users may only open the views in ``TENANT_VIEW_ALLOWLIST``;
  ``PLATFORM_VIEW`` marks platform-only actions.
- ``scoped_tenant()`` forces a tenant-bound user's own tenant on filtered views (another ``tenant_id`` is refused);
  ``ensure_tenant_access()`` checks the tenant of a single run / review / budget before anything is shown or done.
- A refusal raises ``DashboardForbidden``: ``app.main`` renders ``forbidden.html`` (403) for GET / HEAD and a 403 JSON
  error for anything else, so nothing runs. Each refusal increments ``dashboard_access_denied_total{reason}`` and
  writes an ``access.denied`` governance event (tenant ``system``; at most one per session and permission per minute,
  counted per process).
- ``actor_for()`` (D23): ``user:<username>`` in ``session`` mode; ``off`` mode keeps the historical strings.

CSRF and re-authentication arrive in P4.
"""

from __future__ import annotations

import logging
import time
from typing import Any, NoReturn
from urllib.parse import quote, urlsplit

from fastapi import Depends, Request
from fastapi.responses import Response
from jinja2 import pass_context
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.infra.postgres import get_session
from app.observability.metrics import DASHBOARD_ACCESS_DENIED_TOTAL
from app.services.access import sessions, users
from app.services.access.permissions import PERMISSIONS, has_permission, view_allowed
from app.services.access.principal import LOCAL_ADMIN, Principal, principal_for

logger = logging.getLogger(__name__)

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


# --- P3: authorization -------------------------------------------------------------------------------------------

PERMISSION_ATTR = "__dashboard_permission__"
PUBLIC = "public"  # marker for routes reachable without a principal (sign-in, sign-out)
UNDECLARED = "undeclared"

REASON_MISSING_PERMISSION = "missing_permission"
REASON_TENANT_SCOPE = "tenant_scope"
REASON_PLATFORM_VIEW = "platform_view"
REASON_UNDECLARED = "permission_undeclared"
ERROR_CODES = {
    REASON_MISSING_PERMISSION: "permission_denied",
    REASON_TENANT_SCOPE: "tenant_scope_denied",
    REASON_PLATFORM_VIEW: "platform_view_denied",
    REASON_UNDECLARED: "permission_undeclared",
}
DENIAL_EVENT_WINDOW_SECONDS = 60.0
_DENIAL_LOG_MAX = 4096
_denial_log: dict[tuple[str, str], float] = {}
_route_permissions: dict[int, str | None] = {}


class DashboardForbidden(Exception):
    """The principal may not do this (missing permission, other tenant, platform view or undeclared route)."""

    def __init__(self, permission: str, reason: str) -> None:
        super().__init__(f"Dashboard access denied ({reason}): {permission}")
        self.permission = permission
        self.reason = reason

    @property
    def code(self) -> str:
        return ERROR_CODES.get(self.reason, "permission_denied")


def reset_denial_log() -> None:
    """Tests: forget the per-minute ``access.denied`` de-duplication."""
    _denial_log.clear()


def _should_record(key: tuple[str, str]) -> bool:
    now = time.monotonic()
    last = _denial_log.get(key)
    if last is not None and now - last < DENIAL_EVENT_WINDOW_SECONDS:
        return False
    if len(_denial_log) >= _DENIAL_LOG_MAX:
        cutoff = now - DENIAL_EVENT_WINDOW_SECONDS
        for stale in [item for item, seen in _denial_log.items() if seen < cutoff]:
            _denial_log.pop(stale, None)
        if len(_denial_log) >= _DENIAL_LOG_MAX:
            _denial_log.clear()
    _denial_log[key] = now
    return True


async def deny(request: Request, session: AsyncSession, principal: Principal, permission: str, reason: str) -> NoReturn:
    """Count, record (``access.denied``, rate limited) and raise ``DashboardForbidden``. Call before any write."""
    DASHBOARD_ACCESS_DENIED_TOTAL.labels(reason=reason).inc()
    if _should_record((principal.session_id or principal.user_id, permission)):
        try:
            users.access_event(
                session,
                "access.denied",
                actor=principal.actor,
                decision="deny",
                reason=f"{principal.username} ({principal.role}) was refused {permission} ({reason}).",
                details={
                    "permission": permission,
                    "reason": reason,
                    "method": request.method,
                    "path": request.url.path,
                    "role": principal.role,
                    "user_tenant_id": principal.tenant_id,
                    "session_id": principal.session_id,
                },
            )
            await session.commit()
        except Exception:  # pragma: no cover - the refusal itself must never fail
            logger.exception("could not record access.denied")
            await session.rollback()
    raise DashboardForbidden(permission, reason)


def principal_of(request: Request) -> Principal:
    """The principal ``dashboard_principal`` attached to this request (``LOCAL_ADMIN`` when none)."""
    principal = getattr(request.state, "principal", None)
    return principal if isinstance(principal, Principal) else LOCAL_ADMIN


def actor_for(request: Request, fallback: str) -> str:
    """D23: ``user:<username>`` in ``session`` mode; ``off`` mode keeps the historical string (``fallback``)."""
    principal = principal_of(request)
    return fallback if principal.synthetic else principal.actor


async def ensure_permission(request: Request, session: AsyncSession, permission: str) -> Principal:
    """An extra check inside a handler (for example ``pricing.import_apply`` when the form asks for ``apply``)."""
    principal = principal_of(request)
    if not has_permission(principal.role, permission):
        await deny(request, session, principal, permission, REASON_MISSING_PERMISSION)
    return principal


async def scoped_tenant(
    request: Request, session: AsyncSession, requested: str | None, *, permission: str = "dashboard.view"
) -> str | None:
    """D22 (a): platform users keep their filter; tenant-bound users always get their own tenant, and asking for
    another tenant is refused (403, not an empty page)."""
    principal = principal_of(request)
    requested = (requested or "").strip() or None
    if principal.is_platform:
        return requested
    if requested is not None and requested != principal.tenant_id:
        await deny(request, session, principal, permission, REASON_TENANT_SCOPE)
    return principal.tenant_id


async def ensure_tenant_access(
    request: Request, session: AsyncSession, tenant_id: str | None, *, permission: str = "dashboard.view"
) -> None:
    """D22 (b): a single run / review / budget of another tenant answers 403 without showing anything."""
    principal = principal_of(request)
    if principal.is_platform:
        return
    if (tenant_id or "") != principal.tenant_id:
        await deny(request, session, principal, permission, REASON_TENANT_SCOPE)


def declared_permission(route: Any) -> str | None:
    """The permission (or ``PUBLIC``) a route declares through its dependencies, ``None`` when it declares none."""
    key = id(route)
    if key in _route_permissions:
        return _route_permissions[key]
    found: str | None = None
    stack = [getattr(route, "dependant", None)]
    while stack:
        node = stack.pop()
        if node is None:
            continue
        marker = getattr(getattr(node, "call", None), PERMISSION_ATTR, None)
        if marker:
            found = marker
            break
        stack.extend(getattr(node, "dependencies", None) or [])
    _route_permissions[key] = found
    return found


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
        await _require_declaration(request, session, LOCAL_ADMIN)
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
    await _require_declaration(request, session, principal)
    return principal


async def _require_declaration(request: Request, session: AsyncSession, principal: Principal) -> None:
    """D19 default deny: a guarded route without ``require_permission`` is refused. ``scope["route"]`` is set by
    FastAPI's router; when it is absent the contract test (``tests/test_v161_dashboard_permissions.py``) still applies."""
    route = request.scope.get("route")
    if route is not None and declared_permission(route) is None:
        await deny(request, session, principal, UNDECLARED, REASON_UNDECLARED)


def require_permission(permission: str, *, view: str | None = None):
    """Route dependency (D19): the principal's role must hold ``permission`` and ``view`` must be open to it (D22)."""
    if permission not in PERMISSIONS:
        raise ValueError(f"Unknown Dashboard permission: {permission}")

    async def dependency(
        request: Request,
        principal: Principal = Depends(dashboard_principal),
        session: AsyncSession = Depends(get_session),
    ) -> Principal:
        if not has_permission(principal.role, permission):
            await deny(request, session, principal, permission, REASON_MISSING_PERMISSION)
        if not view_allowed(principal.tenant_id, view):
            await deny(request, session, principal, permission, REASON_PLATFORM_VIEW)
        return principal

    setattr(dependency, PERMISSION_ATTR, permission)
    dependency.dashboard_view = view  # type: ignore[attr-defined]
    return dependency


def needs(permission: str, *, view: str | None = None) -> list[Any]:
    """Decorator shorthand: ``@router.get(path, dependencies=needs("dashboard.view", view="runs"))``."""
    return [Depends(require_permission(permission, view=view))]


def public_route():
    """Marks a route that is deliberately reachable without a principal (sign-in, sign-out)."""

    async def dependency() -> None:
        return None

    setattr(dependency, PERMISSION_ATTR, PUBLIC)
    return dependency


def template_principal(request: Any) -> Principal | None:
    """Jinja global: the principal of the request being rendered (``None`` on pages outside the guard)."""
    state = getattr(request, "state", None)
    return getattr(state, "principal", None) if state is not None else None


@pass_context
def template_can(context: Any, permission: str, view: str | None = None) -> bool:
    """Jinja global ``can("<permission>", view=None)``: hides buttons and links only; the server re-checks."""
    principal = template_principal(context.get("request"))
    if principal is None:
        return False
    return has_permission(principal.role, permission) and view_allowed(principal.tenant_id, view)


def install_template_globals(templates: Any) -> None:
    templates.env.globals["dashboard_user"] = template_principal
    templates.env.globals["dashboard_auth_mode"] = lambda: get_settings().dashboard_auth_mode
    templates.env.globals["can"] = template_can
