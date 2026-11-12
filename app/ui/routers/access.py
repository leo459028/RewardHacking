"""v1.6.1 Dashboard sign-in, account and user management (plan D9-D18, feature five).

- ``/dashboard/login`` (GET / POST), ``/dashboard/logout`` (POST): sign-in through the providers in
  ``app.services.access.providers``; a new session on every sign-in; the same generic message for every failure.
- ``/dashboard/account`` and ``/dashboard/account/password``: one's own account and password change (required first
  when ``must_change_password`` is set). A password change revokes every session and signs this browser in again.
- ``/dashboard/admin/users``: create users, change roles, disable / enable, reset passwords, unlock, revoke sessions.

Only ``DASHBOARD_AUTH_MODE=session`` has accounts. In ``off`` mode the sign-in pages redirect to the Dashboard and the
user-management actions are refused (``403 dashboard_auth_off``), so nobody can create accounts without signing in.
P3: the sign-in pages are ``public_route()``; the account pages need ``account.self`` and user management
``users.manage`` (admin, platform view).
P4 (D24, D26): every POST here passes ``csrf_protect`` (router dependency); the sign-in form gets a signed
``rh_login_csrf`` cookie (reused while it has at least 5 minutes left, P8). ``/dashboard/reauth`` re-checks the password
(``provider.reauthenticate()``; failures count towards the lockout), writes ``access.reauth`` and sends the user back
to the page they came from, where they submit the action again (the original POST is never replayed). User
management actions need a re-authentication within ``DASHBOARD_REAUTH_MAX_AGE_SECONDS`` (``users.manage`` is a
step-up permission).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.infra.postgres import get_session
from app.observability.metrics import DASHBOARD_ACCESS_DENIED_TOTAL, DASHBOARD_LOGINS_TOTAL
from app.services.access import csrf, permissions, providers, sessions, users
from app.services.access.errors import AccessError
from app.services.access.principal import Principal
from app.ui.routes import templates
from app.ui.security import (
    ACCOUNT_PATH,
    LOGIN_PATH,
    REAUTH_PATH,
    clear_session_cookie,
    csrf_protect,
    current_session,
    login_csrf_key,
    public_route,
    require_permission,
    safe_next,
    set_session_cookie,
)

router = APIRouter(tags=["dashboard"], dependencies=[Depends(csrf_protect)])

USERS_PAGE = "/dashboard/admin/users"
GENERIC_LOGIN_ERROR = "Invalid username or password. | 帳號或密碼錯誤。"
EXPIRED_LOGIN_FORM = "The sign-in form expired. Please sign in again. | 登入表單已過期，請重新登入。"
GENERIC_REAUTH_ERROR = "Password not accepted. | 密碼錯誤或帳號已鎖定。"


def _session_mode() -> bool:
    return get_settings().dashboard_auth_mode == "session"


def _redirect(path: str, **params: str) -> RedirectResponse:
    query = urlencode({key: value for key, value in params.items() if value})
    return RedirectResponse(f"{path}?{query}" if query else path, status_code=303)


def _auth_off() -> HTMLResponse:
    return HTMLResponse(
        "Dashboard sign-in is off (DASHBOARD_AUTH_MODE=off); accounts are managed only in session mode.", status_code=403
    )


def _login_page(request: Request, *, next_path: str, error: str = "", username: str = "", status_code: int = 200) -> HTMLResponse:
    """Renders the sign-in form with its CSRF pair: the signed cookie and the matching hidden form token. A still-valid
    cookie is reused (P8: a second rendering must not invalidate the form already shown); otherwise a fresh pair."""
    settings = get_settings()
    existing = request.cookies.get(csrf.LOGIN_COOKIE_NAME)
    reused = csrf.login_token_from_cookie(existing, login_csrf_key(settings))
    if reused is not None and existing:
        cookie_value, form_token = existing, reused
    else:
        cookie_value, form_token = csrf.new_login_token(login_csrf_key(settings))
    request.state.login_csrf_token = form_token
    response = templates.TemplateResponse(
        request,
        "login.html",
        {
            "bare": True,
            "providers": providers.get_providers(settings),
            "next_path": next_path,
            "error": error,
            "username": username,
        },
        status_code=status_code,
    )
    response.set_cookie(
        csrf.LOGIN_COOKIE_NAME,
        cookie_value,
        max_age=csrf.LOGIN_TOKEN_TTL_SECONDS,
        path=csrf.LOGIN_COOKIE_PATH,
        secure=settings.dashboard_cookie_secure_effective,
        httponly=True,
        samesite="strict",
    )
    return response


def expired_login_form(request: Request, next_path: str) -> HTMLResponse:
    """``LoginCsrfInvalid`` (``app.main``): show the form again with a fresh token; nothing was checked or counted."""
    return _login_page(request, next_path=safe_next(next_path), error=EXPIRED_LOGIN_FORM, status_code=403)


# ---------------------------------------------------------------------------
# sign-in / sign-out
# ---------------------------------------------------------------------------


@router.get("/dashboard/login", response_class=HTMLResponse, dependencies=[Depends(public_route())])
async def login_page(request: Request, session: AsyncSession = Depends(get_session)):
    next_path = safe_next(request.query_params.get("next"))
    if not _session_mode():
        return RedirectResponse(next_path, status_code=303)
    if await current_session(request, session, get_settings()) is not None:
        return RedirectResponse(next_path, status_code=303)
    return _login_page(request, next_path=next_path)


@router.post("/dashboard/login", response_class=HTMLResponse, dependencies=[Depends(public_route())])
async def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    next: str = Form(""),
    provider: str = Form(providers.LocalPasswordProvider.name),
    session: AsyncSession = Depends(get_session),
):
    settings = get_settings()
    next_path = safe_next(next)
    if not _session_mode():
        return RedirectResponse(next_path, status_code=303)
    chosen = providers.get_provider(settings, provider) or providers.get_providers(settings)[0]
    result = await chosen.authenticate(session, settings, {"username": username, "password": password})
    user = await providers.resolve_user(session, result.identity) if result.ok else None
    if user is None:
        await session.commit()  # failed-attempt counter and a possible lockout event
        DASHBOARD_LOGINS_TOTAL.labels(result=result.reason or "invalid_credentials").inc()
        # D17: one message and one status for unknown users, wrong passwords, locked and disabled accounts.
        return _login_page(request, next_path=next_path, error=GENERIC_LOGIN_ERROR, username=username.strip()[:64])
    token, row = await sessions.create_session(
        session, settings, user, auth_method=getattr(chosen, "auth_method", chosen.name), user_agent=request.headers.get("user-agent")
    )
    users.access_event(
        session,
        "access.login",
        actor=f"user:{user.username}",
        reason=f"{user.username} signed in.",
        details={"target_user_id": user.user_id, "session_id": row.session_id, "auth_method": row.auth_method,
                 "provider": chosen.name, "must_change_password": bool(user.must_change_password)},
    )
    await session.commit()
    DASHBOARD_LOGINS_TOTAL.labels(result="success").inc()
    destination = _redirect(ACCOUNT_PATH, must_change="1", next=next_path) if user.must_change_password else RedirectResponse(next_path, status_code=303)
    set_session_cookie(destination, token, settings)
    destination.delete_cookie(
        csrf.LOGIN_COOKIE_NAME, path=csrf.LOGIN_COOKIE_PATH, secure=settings.dashboard_cookie_secure_effective, httponly=True, samesite="strict"
    )
    return destination


@router.post("/dashboard/logout", dependencies=[Depends(public_route())])
async def logout(request: Request, session: AsyncSession = Depends(get_session)):
    settings = get_settings()
    response: Response = RedirectResponse(LOGIN_PATH if _session_mode() else "/dashboard", status_code=303)
    lookup = await current_session(request, session, settings) if _session_mode() else None
    if lookup is not None:
        await sessions.revoke_session(session, lookup.session_row, reason="logout")
        users.access_event(
            session,
            "access.logout",
            actor=f"user:{lookup.user.username}",
            reason=f"{lookup.user.username} signed out.",
            details={"target_user_id": lookup.user.user_id, "session_id": lookup.session_row.session_id},
        )
        await session.commit()
    clear_session_cookie(response, settings)
    return response


# ---------------------------------------------------------------------------
# re-authentication (D26)
# ---------------------------------------------------------------------------


def _reauth_page(request: Request, principal: Principal, *, next_path: str, error: str = "") -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "reauth.html",
        {"active": "account", "principal": principal, "next_path": next_path, "error": error},
    )


@router.get(REAUTH_PATH, response_class=HTMLResponse)
async def reauth_page(request: Request, principal: Principal = Depends(require_permission("account.self", view="account"))):
    next_path = safe_next(request.query_params.get("next"))
    if principal.synthetic:
        return RedirectResponse(next_path, status_code=303)
    return _reauth_page(request, principal, next_path=next_path)


@router.post(REAUTH_PATH, response_class=HTMLResponse)
async def reauth_submit(
    request: Request,
    password: str = Form(""),
    next: str = Form(""),
    principal: Principal = Depends(require_permission("account.self", view="account")),
    session: AsyncSession = Depends(get_session),
):
    settings = get_settings()
    next_path = safe_next(next)
    if principal.synthetic:
        return RedirectResponse(next_path, status_code=303)
    user = await users.get_user(session, principal.user_id)
    row = request.state.dashboard_session
    provider = providers.get_provider(settings, providers.LocalPasswordProvider.name)
    result = await provider.reauthenticate(session, settings, user, {"password": password})
    if not result.ok:
        await session.commit()  # the failed attempt (and a possible lockout event) counts like a sign-in failure
        DASHBOARD_ACCESS_DENIED_TOTAL.labels(reason="reauth_failed").inc()
        return _reauth_page(request, principal, next_path=next_path, error=GENERIC_REAUTH_ERROR)
    await sessions.mark_reauthenticated(session, row)
    users.access_event(
        session,
        "access.reauth",
        actor=principal.actor,
        reason=f"{principal.username} re-authenticated.",
        details={"target_user_id": principal.user_id, "session_id": row.session_id, "auth_method": row.auth_method, "next": next_path},
    )
    await session.commit()
    return RedirectResponse(next_path, status_code=303)


# ---------------------------------------------------------------------------
# own account
# ---------------------------------------------------------------------------


@router.get("/dashboard/account", response_class=HTMLResponse)
async def account_page(request: Request, principal: Principal = Depends(require_permission("account.self", view="account")), session: AsyncSession = Depends(get_session)):
    live_sessions: list[Any] = []
    if not principal.synthetic:
        live_sessions = await sessions.list_user_sessions(session, principal.user_id)
    return templates.TemplateResponse(
        request,
        "account.html",
        {
            "active": "account",
            "principal": principal,
            "sessions": live_sessions,
            "must_change": principal.must_change_password,
            "next_path": safe_next(request.query_params.get("next")),
            "notice": request.query_params.get("notice", ""),
            "error": request.query_params.get("error", ""),
            "min_length": get_settings().dashboard_password_min_length,
        },
    )


@router.post("/dashboard/account/password")
async def account_password(
    request: Request,
    current_password: str = Form(""),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
    next: str = Form(""),
    principal: Principal = Depends(require_permission("account.self", view="account")),
    session: AsyncSession = Depends(get_session),
):
    settings = get_settings()
    if principal.synthetic:
        return _auth_off()
    user = await users.get_user(session, principal.user_id)
    if new_password != confirm_password:
        return _redirect(ACCOUNT_PATH, error="password_mismatch", next=next)
    provider = providers.get_provider(settings, providers.LocalPasswordProvider.name)
    check = await provider.reauthenticate(session, settings, user, {"password": current_password})
    if not check.ok:
        await session.commit()  # a wrong current password counts towards the lockout
        return _redirect(ACCOUNT_PATH, error="current_password_invalid", next=next)
    was_forced = bool(user.must_change_password)
    try:
        await users.set_password(
            session, settings, user_id=user.user_id, new_password=new_password, actor=principal.actor, must_change_password=False
        )
    except AccessError as exc:
        await session.rollback()
        return _redirect(ACCOUNT_PATH, error=exc.code, next=next)
    # Every session was revoked (D15); keep this browser signed in with a brand-new session.
    token, _row = await sessions.create_session(
        session, settings, user, auth_method=principal.auth_method, user_agent=request.headers.get("user-agent")
    )
    await session.commit()
    response = RedirectResponse(safe_next(next), status_code=303) if was_forced else _redirect(ACCOUNT_PATH, notice="password_changed")
    set_session_cookie(response, token, settings)
    return response


# ---------------------------------------------------------------------------
# user management
# ---------------------------------------------------------------------------


@router.get("/dashboard/admin/users", response_class=HTMLResponse)
async def users_page(request: Request, principal: Principal = Depends(require_permission("users.manage", view="users")), session: AsyncSession = Depends(get_session)):
    rows = [users.user_view(user) for user in await users.list_users(session)] if _session_mode() else []
    return templates.TemplateResponse(
        request,
        "admin_users.html",
        {
            "active": "users",
            "principal": principal,
            "session_mode": _session_mode(),
            "users": rows,
            "roles": permissions.ROLES,
            "notice": request.query_params.get("notice", ""),
            "error": request.query_params.get("error", ""),
            "min_length": get_settings().dashboard_password_min_length,
        },
    )


async def _user_action(session: AsyncSession, action) -> RedirectResponse:
    try:
        notice = await action()
    except AccessError as exc:
        await session.rollback()
        return _redirect(USERS_PAGE, error=exc.code)
    await session.commit()
    return _redirect(USERS_PAGE, notice=notice)


@router.post("/dashboard/admin/users")
async def users_create(
    username: str = Form(""),
    display_name: str = Form(""),
    role: str = Form(""),
    tenant_id: str = Form(""),
    password: str = Form(""),
    principal: Principal = Depends(require_permission("users.manage", view="users")),
    session: AsyncSession = Depends(get_session),
):
    if principal.synthetic:
        return _auth_off()

    async def action() -> str:
        user = await users.create_user(
            session, get_settings(), username=username, password=password, role=role, tenant_id=tenant_id,
            display_name=display_name, actor=principal.actor,
        )
        return f"created:{user.username}"

    return await _user_action(session, action)


@router.post("/dashboard/admin/users/{user_id}/role")
async def users_role(
    user_id: str,
    role: str = Form(""),
    tenant_id: str = Form(""),
    principal: Principal = Depends(require_permission("users.manage", view="users")),
    session: AsyncSession = Depends(get_session),
):
    if principal.synthetic:
        return _auth_off()

    async def action() -> str:
        user = await users.change_role(
            session, user_id=user_id, role=role, tenant_id=tenant_id, actor=principal.actor, actor_user_id=principal.user_id
        )
        return f"role_changed:{user.username}"

    return await _user_action(session, action)


@router.post("/dashboard/admin/users/{user_id}/status")
async def users_status(
    user_id: str,
    status: str = Form(""),
    principal: Principal = Depends(require_permission("users.manage", view="users")),
    session: AsyncSession = Depends(get_session),
):
    if principal.synthetic:
        return _auth_off()

    async def action() -> str:
        user = await users.set_status(session, user_id=user_id, status=status, actor=principal.actor, actor_user_id=principal.user_id)
        return f"status_{user.status}:{user.username}"

    return await _user_action(session, action)


@router.post("/dashboard/admin/users/{user_id}/password")
async def users_reset_password(
    user_id: str,
    new_password: str = Form(""),
    principal: Principal = Depends(require_permission("users.manage", view="users")),
    session: AsyncSession = Depends(get_session),
):
    if principal.synthetic:
        return _auth_off()

    async def action() -> str:
        if user_id == principal.user_id:
            raise AccessError("use_account_page", "Change your own password on the account page.", status_code=403)
        user = await users.set_password(
            session, get_settings(), user_id=user_id, new_password=new_password, actor=principal.actor, must_change_password=True
        )
        return f"password_reset:{user.username}"

    return await _user_action(session, action)


@router.post("/dashboard/admin/users/{user_id}/unlock")
async def users_unlock(user_id: str, principal: Principal = Depends(require_permission("users.manage", view="users")), session: AsyncSession = Depends(get_session)):
    if principal.synthetic:
        return _auth_off()

    async def action() -> str:
        user = await users.unlock(session, user_id=user_id, actor=principal.actor)
        return f"unlocked:{user.username}"

    return await _user_action(session, action)


@router.post("/dashboard/admin/users/{user_id}/sessions/revoke")
async def users_revoke_sessions(user_id: str, principal: Principal = Depends(require_permission("users.manage", view="users")), session: AsyncSession = Depends(get_session)):
    if principal.synthetic:
        return _auth_off()

    async def action() -> str:
        user = await users.get_user(session, user_id)
        if user is None:
            raise AccessError("user_not_found", "User not found.", status_code=404)
        revoked = await sessions.revoke_user_sessions(session, user.user_id, reason="admin_revoked")
        users.access_event(
            session,
            "access.session_revoked",
            actor=principal.actor,
            reason=f"Revoked {revoked} session(s) of {user.username}.",
            details={"target_user_id": user.user_id, "username": user.username, "sessions_revoked": revoked},
        )
        return f"sessions_revoked:{user.username}"

    return await _user_action(session, action)
