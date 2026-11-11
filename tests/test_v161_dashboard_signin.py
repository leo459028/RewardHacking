"""v1.6.1 P2: Dashboard sign-in, sessions, account and user management (plan D9-D18, D45, feature five).

``tests/conftest.py`` pins ``DASHBOARD_AUTH_MODE=off``; these tests switch to ``session`` mode with the ``signed_in``
fixture (or ``session_mode``). Passwords are generated at run time. Roles are not enforced yet (P3): every signed-in
user acts as ``admin`` here.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from datetime import datetime, timedelta, timezone

import pytest
from prometheus_client import REGISTRY
from sqlalchemy import select

from app.core.config import get_settings
from app.models.access import DashboardSession, DashboardUser
from app.models.governance_event import GovernanceEvent
from app.services.access import sessions

LOGIN = "/dashboard/login"
USERS = "/dashboard/admin/users"
GENERIC_ERROR = "Invalid username or password."
PAGES = (
    "/dashboard",
    "/dashboard/runs",
    "/dashboard/approvals",
    "/dashboard/reviews",
    "/dashboard/benchmark",
    "/dashboard/judge-profiles",
    "/dashboard/worker",
    "/dashboard/observability",
    "/dashboard/governance",
    "/dashboard/control-plane",
    "/dashboard/costs",
    "/dashboard/admin/finops/pricing",
    "/dashboard/operations",
    "/dashboard/retention",
    "/dashboard/playground",
    "/dashboard/account",
    USERS,
)


def _db(session_factory, fn):
    async def scenario():
        async with session_factory() as session:
            return await fn(session)

    return asyncio.run(scenario())


def _all(session, stmt):
    async def run():
        return list((await session.scalars(stmt)).all())

    return run()


def _events(session_factory, prefix="access."):
    return _db(session_factory, lambda s: _all(s, select(GovernanceEvent).where(GovernanceEvent.event_type.like(f"{prefix}%")).order_by(GovernanceEvent.id)))


def _user(session_factory, username):
    return _db(session_factory, lambda s: s.scalar(select(DashboardUser).where(DashboardUser.username == username)))


def _login(client, username, password, next_path="/dashboard"):
    return client.post(LOGIN, data={"username": username, "password": password, "next": next_path}, follow_redirects=False)


def _account(signed_in, role="admin", **kwargs):
    """A user with a known password and no cookie on the client (signed out)."""
    who = signed_in(role=role, **kwargs)
    who.client.cookies.clear()
    return who


def _metric(name, **labels):
    return REGISTRY.get_sample_value(name, labels) or 0.0


@pytest.fixture
def session_mode(monkeypatch):
    monkeypatch.setenv("DASHBOARD_AUTH_MODE", "session")
    monkeypatch.setenv("DASHBOARD_PASSWORD_SCRYPT_N", "16")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# signed out
# ---------------------------------------------------------------------------


def test_signed_out_pages_redirect_to_login_with_next(client, session_mode):
    response = client.get("/dashboard/runs?tenant_id=t-1&status=queued", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard/login?next=%2Fdashboard%2Fruns%3Ftenant_id%3Dt-1%26status%3Dqueued"


@pytest.mark.parametrize("path", PAGES)
def test_every_dashboard_page_needs_sign_in(client, session_mode, path):
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].startswith(LOGIN + "?next=")


@pytest.mark.parametrize(
    "path",
    ["/dashboard/approvals/expire-stale", "/dashboard/control-plane/workflows", USERS, "/dashboard/account/password", "/dashboard/retention/holds"],
)
def test_signed_out_posts_get_401_and_nothing_runs(client, session_mode, session_factory, path):
    response = client.post(path, data={"workflow_name": "x", "username": "intruder", "password": "x" * 20}, follow_redirects=False)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "dashboard_sign_in_required"
    assert _user(session_factory, "intruder") is None


def test_unknown_or_expired_cookie_is_treated_as_signed_out(client, session_mode):
    client.cookies.set(sessions.COOKIE_NAME, "forged-token")
    assert client.get("/dashboard", follow_redirects=False).status_code == 303


def test_login_page_renders_provider_form(client, session_mode):
    page = client.get(LOGIN + "?next=%2Fdashboard%2Freviews")
    assert page.status_code == 200
    assert 'action="/dashboard/login"' in page.text and 'data-provider="local"' in page.text
    assert 'name="next" value="/dashboard/reviews"' in page.text
    assert 'data-testid="sidebar-user"' not in page.text and 'href="/dashboard/runs"' not in page.text  # bare page


# ---------------------------------------------------------------------------
# sign-in
# ---------------------------------------------------------------------------


def test_successful_login_sets_hardened_cookie_and_stores_only_the_hash(client, signed_in, session_factory):
    who = _account(signed_in, role="reviewer")
    before = _metric("dashboard_logins_total", result="success")
    response = _login(client, who.username, who.password, "/dashboard/reviews")
    assert response.status_code == 303 and response.headers["location"] == "/dashboard/reviews"
    cookie = response.headers["set-cookie"]
    token = cookie.split(";")[0].split("=", 1)[1]
    lowered = cookie.lower()
    assert "httponly" in lowered and "samesite=lax" in lowered and "path=/dashboard" in lowered and "secure" not in lowered
    rows = _db(session_factory, lambda s: _all(s, select(DashboardSession).where(DashboardSession.user_id == who.user_id)))
    assert any(row.session_hash == sessions.token_hash(token) for row in rows) and all(row.session_hash != token for row in rows)
    assert client.get("/dashboard/reviews").status_code == 200
    login_events = [event for event in _events(session_factory) if event.event_type == "access.login"]
    assert login_events[-1].user_id == f"user:{who.username}" and login_events[-1].tenant_id == "system"
    assert token not in json.dumps(login_events[-1].details) and who.password not in json.dumps(login_events[-1].details)
    assert _metric("dashboard_logins_total", result="success") == before + 1


def test_cookie_is_secure_outside_dev(client, signed_in, monkeypatch):
    who = _account(signed_in)
    monkeypatch.setenv("APP_ENV", "staging")
    get_settings.cache_clear()
    response = _login(client, who.username, who.password)
    assert response.status_code == 303 and "secure" in response.headers["set-cookie"].lower()


def test_every_login_issues_a_new_session(client, signed_in, session_factory):
    who = _account(signed_in)
    client.cookies.set(sessions.COOKIE_NAME, "attacker-chosen-value")
    first = _login(client, who.username, who.password).headers["set-cookie"].split(";")[0]
    client.cookies.clear()
    second = _login(client, who.username, who.password).headers["set-cookie"].split(";")[0]
    assert first != second and "attacker-chosen-value" not in (first + second)
    hashes = {row.session_hash for row in _db(session_factory, lambda s: _all(s, select(DashboardSession)))}
    assert sessions.token_hash("attacker-chosen-value") not in hashes


@pytest.mark.parametrize(
    "next_path",
    ["https://evil.example/x", "//evil.example", "/\\evil.example", "/other", "javascript:alert(1)", "/dashboard/login", "/dashboard/../v1/runs", ""],
)
def test_next_parameter_cannot_redirect_off_the_dashboard(client, signed_in, next_path):
    who = _account(signed_in)
    response = _login(client, who.username, who.password, next_path)
    assert response.status_code == 303 and response.headers["location"] == "/dashboard"


def test_signed_in_visit_to_login_page_goes_to_next(client, signed_in):
    signed_in()
    response = client.get(LOGIN + "?next=%2Fdashboard%2Fcosts", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/dashboard/costs"


def test_failures_look_identical_for_unknown_wrong_disabled_and_locked(client, signed_in, session_factory):
    admin = signed_in(username="root")
    victim = _account(signed_in, role="operator", username="victim")
    disabled = _account(signed_in, role="operator", username="gone")
    client.cookies.set(sessions.COOKIE_NAME, admin.token)
    assert client.post(f"{USERS}/{disabled.user_id}/status", data={"status": "disabled"}, follow_redirects=False).status_code == 303
    client.cookies.clear()
    responses = {
        "unknown": _login(client, "ghost", victim.password),
        "wrong": _login(client, "victim", "wrong-" + victim.password),
        "disabled": _login(client, "gone", disabled.password),
    }
    bodies = {key: (resp.status_code, resp.text.replace(f'value="{name}"', 'value="X"'))
              for (key, resp), name in zip(responses.items(), ("ghost", "victim", "gone"))}
    assert len(set(bodies.values())) == 1
    status, body = bodies["unknown"]
    assert status == 200 and GENERIC_ERROR in body and "set-cookie" not in responses["unknown"].headers


def test_lockout_after_five_failures_refuses_the_right_password(client, signed_in, session_factory):
    who = _account(signed_in, role="operator")
    for attempt in range(5):
        assert GENERIC_ERROR in _login(client, who.username, f"bad-{attempt}-{who.password}").text
    locked = _login(client, who.username, who.password)
    assert locked.status_code == 200 and GENERIC_ERROR in locked.text and "set-cookie" not in locked.headers
    user = _user(session_factory, who.username)
    assert user.locked_until is not None
    lockouts = [event for event in _events(session_factory) if event.event_type == "access.lockout"]
    assert len(lockouts) == 1 and lockouts[0].decision == "deny"
    # Individual failures are not written as governance events (D18).
    assert not [event for event in _events(session_factory) if event.event_type == "access.login_failed"]


def test_admin_unlock_lets_the_user_sign_in_again(client, signed_in, session_factory):
    who = _account(signed_in, role="operator")
    for attempt in range(5):
        _login(client, who.username, f"bad-{attempt}")
    admin = signed_in()
    assert client.post(f"{USERS}/{who.user_id}/unlock", follow_redirects=False).headers["location"] == f"{USERS}?notice=unlocked%3A{who.username}"
    client.cookies.clear()
    assert _login(client, who.username, who.password).status_code == 303
    assert admin.username != who.username


# ---------------------------------------------------------------------------
# sessions end
# ---------------------------------------------------------------------------


def test_logout_revokes_session_and_clears_cookie(client, signed_in, session_factory):
    who = signed_in()
    response = client.post("/dashboard/logout", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == LOGIN
    assert 'rh_session=""' in response.headers["set-cookie"] or "max-age=0" in response.headers["set-cookie"].lower()
    client.cookies.set(sessions.COOKIE_NAME, who.token)
    assert client.get("/dashboard", follow_redirects=False).status_code == 303
    row = _db(session_factory, lambda s: s.scalar(select(DashboardSession).where(DashboardSession.session_id == who.session_id)))
    assert row.revoked_reason == "logout"
    assert [event.event_type for event in _events(session_factory)][-1] == "access.logout"


def test_idle_and_absolute_timeouts_end_the_session(client, signed_in, session_factory):
    who = signed_in()
    assert client.get("/dashboard").status_code == 200

    async def age(session, **values):
        row = await session.scalar(select(DashboardSession).where(DashboardSession.session_id == who.session_id))
        for key, value in values.items():
            setattr(row, key, value)
        await session.commit()

    now = datetime.now(timezone.utc)
    _db(session_factory, lambda s: age(s, last_seen_at=now - timedelta(minutes=31)))
    assert client.get("/dashboard", follow_redirects=False).status_code == 303
    who = signed_in()  # a fresh session, then push it past the absolute timeout while it stays active
    _db(session_factory, lambda s: age(s, expires_at=now - timedelta(seconds=1)))
    assert client.get("/dashboard", follow_redirects=False).status_code == 303


def test_disabling_a_user_ends_their_session_immediately(client, signed_in):
    victim = signed_in(role="operator")
    signed_in()  # an admin; its cookie is now on the client
    assert client.post(f"{USERS}/{victim.user_id}/status", data={"status": "disabled"}, follow_redirects=False).status_code == 303
    client.cookies.set(sessions.COOKIE_NAME, victim.token)
    assert client.get("/dashboard", follow_redirects=False).status_code == 303


def test_role_change_ends_the_users_sessions(client, signed_in, session_factory):
    target = signed_in(role="operator")
    signed_in()
    response = client.post(f"{USERS}/{target.user_id}/role", data={"role": "reviewer", "tenant_id": ""}, follow_redirects=False)
    assert "notice=role_changed" in response.headers["location"]
    assert _user(session_factory, target.username).role == "reviewer"
    client.cookies.set(sessions.COOKIE_NAME, target.token)
    assert client.get("/dashboard", follow_redirects=False).status_code == 303


# ---------------------------------------------------------------------------
# passwords
# ---------------------------------------------------------------------------


def test_must_change_password_confines_the_user_to_the_account_page(client, signed_in, session_factory):
    who = _account(signed_in, role="reviewer", must_change_password=True)
    login = _login(client, who.username, who.password, "/dashboard/reviews")
    assert login.headers["location"] == "/dashboard/account?must_change=1&next=%2Fdashboard%2Freviews"
    assert client.get("/dashboard/reviews", follow_redirects=False).headers["location"] == "/dashboard/account?must_change=1"
    blocked = client.post("/dashboard/approvals/expire-stale", follow_redirects=False)
    assert blocked.status_code == 403 and blocked.json()["error"]["code"] == "password_change_required"
    assert 'data-testid="must-change"' in client.get("/dashboard/account?must_change=1").text
    new_password = "pw-" + secrets.token_hex(8)
    changed = client.post(
        "/dashboard/account/password",
        data={"current_password": who.password, "new_password": new_password, "confirm_password": new_password, "next": "/dashboard/reviews"},
        follow_redirects=False,
    )
    assert changed.status_code == 303 and changed.headers["location"] == "/dashboard/reviews"
    assert client.get("/dashboard/reviews").status_code == 200
    assert _user(session_factory, who.username).must_change_password is False


def test_password_change_revokes_other_sessions_and_keeps_this_browser(client, signed_in, session_factory):
    who = signed_in(role="operator")
    other_token = _db(session_factory, lambda s: _new_session(s, who.user_id))
    new_password = "pw-" + secrets.token_hex(8)
    response = client.post(
        "/dashboard/account/password",
        data={"current_password": who.password, "new_password": new_password, "confirm_password": new_password},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/dashboard/account?notice=password_changed"
    assert client.get("/dashboard").status_code == 200  # re-issued cookie
    for old in (who.token, other_token):
        client.cookies.set(sessions.COOKIE_NAME, old)
        assert client.get("/dashboard", follow_redirects=False).status_code == 303
    events = [event for event in _events(session_factory) if event.event_type == "access.password_changed"]
    assert events and new_password not in json.dumps(events[-1].details) and events[-1].details["sessions_revoked"] == 2


async def _new_session(session, user_id):
    user = await session.scalar(select(DashboardUser).where(DashboardUser.user_id == user_id))
    token, _ = await sessions.create_session(session, get_settings(), user, auth_method="local_password")
    await session.commit()
    return token


@pytest.mark.parametrize(
    ("current", "new", "confirm", "code"),
    [("WRONG", "fresh-password-1", "fresh-password-1", "current_password_invalid"),
     (None, "fresh-password-1", "different-password", "password_mismatch"),
     (None, "short", "short", "password_too_short"),
     (None, None, None, "password_unchanged")],
)
def test_password_change_refusals(client, signed_in, session_factory, current, new, confirm, code):
    who = signed_in(role="operator")
    data = {"current_password": current or who.password, "new_password": new or who.password, "confirm_password": confirm or who.password}
    response = client.post("/dashboard/account/password", data=data, follow_redirects=False)
    assert response.status_code == 303 and f"error={code}" in response.headers["location"]
    assert client.get("/dashboard").status_code == 200  # still signed in
    if code == "current_password_invalid":
        assert _user(session_factory, who.username).failed_attempts == 1


# ---------------------------------------------------------------------------
# user management
# ---------------------------------------------------------------------------


def test_admin_creates_a_user_who_must_change_password(client, signed_in, session_factory):
    admin = signed_in()
    initial = "pw-" + secrets.token_hex(8)
    response = client.post(
        USERS,
        data={"username": "New.Reviewer", "display_name": "N R", "role": "reviewer", "tenant_id": "reviews-qa-tenant", "password": initial},
        follow_redirects=False,
    )
    assert response.headers["location"] == f"{USERS}?notice=created%3Anew.reviewer"
    user = _user(session_factory, "new.reviewer")
    assert user.must_change_password and user.tenant_id == "reviews-qa-tenant" and user.created_by == f"user:{admin.username}"
    page = client.get(USERS)
    assert 'data-username="new.reviewer"' in page.text and "scrypt$" not in page.text and initial not in page.text
    created = [event for event in _events(session_factory) if event.event_type == "access.user_created"][-1]
    assert created.user_id == f"user:{admin.username}" and initial not in json.dumps(created.details)


@pytest.mark.parametrize(
    ("data", "code"),
    [({"username": "x1", "role": "tenant_admin", "tenant_id": ""}, "tenant_required_for_role"),
     ({"username": "x2", "role": "admin", "tenant_id": "t"}, "role_is_platform_only"),
     ({"username": "x3", "role": "god", "tenant_id": ""}, "invalid_role"),
     ({"username": "bad name", "role": "auditor", "tenant_id": ""}, "invalid_username")],
)
def test_user_creation_refusals_are_shown(client, signed_in, data, code):
    signed_in()
    response = client.post(USERS, data={**data, "password": "pw-" + secrets.token_hex(8)}, follow_redirects=False)
    assert response.headers["location"] == f"{USERS}?error={code}"
    assert f"<code>{code}</code>" in client.get(response.headers["location"]).text


def test_last_admin_and_self_protection(client, signed_in, session_factory):
    me = signed_in()
    for path, data, code in (
        (f"{USERS}/{me.user_id}/status", {"status": "disabled"}, "cannot_disable_self"),
        (f"{USERS}/{me.user_id}/role", {"role": "auditor", "tenant_id": ""}, "cannot_change_own_role"),
        (f"{USERS}/{me.user_id}/password", {"new_password": "pw-" + secrets.token_hex(8)}, "use_account_page"),
    ):
        assert client.post(path, data=data, follow_redirects=False).headers["location"] == f"{USERS}?error={code}"
    other = _account(signed_in, username="second-admin")
    client.cookies.set(sessions.COOKIE_NAME, other.token)
    client.post(f"{USERS}/{me.user_id}/status", data={"status": "disabled"}, follow_redirects=False)
    assert client.post(f"{USERS}/{other.user_id}/status", data={"status": "disabled"}, follow_redirects=False).headers["location"].endswith(
        "error=cannot_disable_self"
    )
    # A second admin may disable the first; the remaining (last) admin still cannot disable itself.
    assert _user(session_factory, me.username).status == "disabled"
    assert 'This is you' in client.get(USERS).text


def test_admin_reset_password_and_revoke_sessions(client, signed_in, session_factory):
    target = signed_in(role="operator")
    admin = signed_in()
    temporary = "pw-" + secrets.token_hex(8)
    reset = client.post(f"{USERS}/{target.user_id}/password", data={"new_password": temporary}, follow_redirects=False)
    assert "notice=password_reset" in reset.headers["location"]
    assert _user(session_factory, target.username).must_change_password is True
    fresh_token = _db(session_factory, lambda s: _new_session(s, target.user_id))
    revoke = client.post(f"{USERS}/{target.user_id}/sessions/revoke", follow_redirects=False)
    assert "notice=sessions_revoked" in revoke.headers["location"]
    client.cookies.set(sessions.COOKIE_NAME, fresh_token)
    assert client.get("/dashboard/account", follow_redirects=False).status_code == 303
    types = [event.event_type for event in _events(session_factory)]
    assert "access.password_changed" in types and types[-1] == "access.session_revoked"
    assert all(temporary not in json.dumps(event.details) + event.reason for event in _events(session_factory))
    assert admin.user_id != target.user_id


def test_sidebar_shows_the_user_and_sign_out(client, signed_in):
    who = signed_in(role="tenant_admin", tenant_id="t-42")
    page = client.get("/dashboard").text
    assert 'data-testid="sidebar-user"' in page and who.username in page and "t-42" in page
    assert 'action="/dashboard/logout"' in page and 'href="/dashboard/admin/users"' in page
    assert 'data-testid="auth-off-banner"' not in page
    account = client.get("/dashboard/account").text
    assert f"user:{who.username}" in account and 'data-testid="account-role">tenant_admin<' in account


@pytest.mark.parametrize("path", PAGES)
def test_every_existing_page_works_when_signed_in(client, signed_in, path):
    signed_in()
    assert client.get(path).status_code == 200


# ---------------------------------------------------------------------------
# off mode (D9, D10)
# ---------------------------------------------------------------------------


def test_off_mode_keeps_todays_behaviour_and_refuses_account_changes(client, session_factory):
    assert get_settings().dashboard_auth_mode == "off"
    page = client.get("/dashboard")
    assert page.status_code == 200 and 'data-testid="auth-off-banner"' in page.text and 'data-testid="sidebar-user"' not in page.text
    assert client.get(LOGIN, follow_redirects=False).headers["location"] == "/dashboard"
    assert client.post("/dashboard/logout", follow_redirects=False).headers["location"] == "/dashboard"
    users_page = client.get(USERS)
    assert users_page.status_code == 200 and "python -m app.jobs.users create" in users_page.text
    created = client.post(USERS, data={"username": "sneaky", "role": "admin", "password": "pw-" + secrets.token_hex(8)})
    assert created.status_code == 403 and _user(session_factory, "sneaky") is None
    assert client.post("/dashboard/account/password", data={"new_password": "x"}).status_code == 403
    assert "no personal account" in client.get("/dashboard/account").text


def test_off_mode_ignores_cookies(client):
    client.cookies.set(sessions.COOKIE_NAME, "anything")
    assert client.get("/dashboard").status_code == 200


# ---------------------------------------------------------------------------
# judge editing gate (D45) and the operations check
# ---------------------------------------------------------------------------


def test_judge_profiles_page_is_editable_once_signed_in(client, signed_in):
    signed_in()
    page = client.get("/dashboard/judge-profiles").text
    assert 'id="judge-read-only"' not in page and "Dashboard sign-in is on" in page


def _readiness_check(client):
    body = client.get("/v1/operations/readiness").json()
    return next(item for item in body["checks"] if item["check_key"] == "dashboard_auth_mode")


def test_operations_check_dashboard_auth_mode(client, session_mode, monkeypatch, signed_in):
    no_admin = _readiness_check(client)
    assert no_admin["status"] == "warn" and "no_active_admin" in no_admin["metadata"]["problems"]
    signed_in()
    assert _readiness_check(client)["status"] == "pass"
    monkeypatch.setenv("DASHBOARD_USERNAME", "ops")
    get_settings.cache_clear()
    legacy = _readiness_check(client)
    assert legacy["status"] == "warn" and "legacy_basic_auth_settings" in legacy["metadata"]["problems"]


def test_operations_check_fails_for_off_mode_in_prod(client, monkeypatch):
    assert _readiness_check(client)["status"] == "pass"  # off in dev: local development
    monkeypatch.setenv("APP_ENV", "prod")
    get_settings.cache_clear()
    check = _readiness_check(client)
    assert check["status"] == "fail" and check["metadata"]["problems"] == ["auth_off_in_prod"]
    get_settings.cache_clear()
