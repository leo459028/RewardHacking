"""v1.6.1 P4: CSRF tokens, re-authentication and Dashboard security headers (plan D24-D27, feature seven).

Sessions come from the ``signed_in`` fixture (``tests/conftest.py``). Its request hook sends the session's token as
``X-CSRF-Token``; tests of a missing token switch it off with ``signed_in.auto_csrf["on"] = False``, and a ``_csrf``
form field (wrong-token tests) takes precedence over the header. Passwords are generated at run time.
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import APIRouter, Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY
from sqlalchemy import func, select

from app.core.config import get_settings
from app.main import DASHBOARD_SECURITY_HEADERS, app
from app.models.access import DashboardSession, DashboardUser
from app.models.base import Base
from app.models.evaluation_review import TenantEvaluationPolicy
from app.models.governance_event import GovernanceEvent
from app.models.tenant import Tenant
from app.services.access import csrf, permissions, sessions
from app.ui import security
from tests.openapi_contract import iter_routes

LOGIN = "/dashboard/login"
REAUTH = "/dashboard/reauth"
TEMPLATES = Path("app/ui/templates")
SAFE = {"GET", "HEAD", "OPTIONS"}


@pytest.fixture(autouse=True)
def _fresh_denial_log():
    security.reset_denial_log()
    yield
    security.reset_denial_log()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _write_routes() -> list[tuple[str, str, str | None]]:
    """``(method, path, declared permission)`` for every non-GET Dashboard route."""
    rows = []
    for route in iter_routes(app.routes):
        path = getattr(route, "path", "")
        if not path.startswith("/dashboard"):
            continue
        for method in sorted(getattr(route, "methods", set()) - SAFE):
            rows.append((method, path, security.declared_permission(route)))
    return sorted(rows)


WRITE_ROUTES = _write_routes()
GUARDED_WRITES = [row for row in WRITE_ROUTES if row[1] != LOGIN]
STEP_UP_ROUTES = [row for row in WRITE_ROUTES if row[2] in permissions.STEP_UP_PERMISSIONS]


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "zz-placeholder", path)


def _db(session_factory, fn):
    async def run():
        async with session_factory() as session:
            return await fn(session)

    return asyncio.run(run())


def _snapshot(session_factory) -> dict[str, int]:
    """Row counts of every table (``access.denied`` events excluded) plus live sessions: a refused request must not
    change any of them."""

    async def run(session):
        counts: dict[str, int] = {}
        for table in Base.metadata.sorted_tables:
            stmt = select(func.count()).select_from(table)
            if table.name == GovernanceEvent.__tablename__:
                stmt = stmt.where(table.c.event_type != "access.denied")
            counts[table.name] = int(await session.scalar(stmt) or 0)
        counts["live_sessions"] = int(
            await session.scalar(select(func.count()).select_from(DashboardSession).where(DashboardSession.revoked_at.is_(None))) or 0
        )
        return counts

    return _db(session_factory, run)


def _session_row(session_factory, session_id: str) -> DashboardSession:
    return _db(session_factory, lambda s: s.scalar(select(DashboardSession).where(DashboardSession.session_id == session_id)))


def _user(session_factory, user_id: str) -> DashboardUser:
    return _db(session_factory, lambda s: s.scalar(select(DashboardUser).where(DashboardUser.user_id == user_id)))


def _age_reauth(session_factory, session_id: str, seconds: int = 301) -> None:
    async def run(session):
        row = await session.scalar(select(DashboardSession).where(DashboardSession.session_id == session_id))
        row.reauth_at = datetime.now(timezone.utc) - timedelta(seconds=seconds)
        await session.commit()

    _db(session_factory, run)


def _events(session_factory, event_type: str) -> list[GovernanceEvent]:
    async def run(session):
        return list((await session.scalars(select(GovernanceEvent).where(GovernanceEvent.event_type == event_type))).all())

    return _db(session_factory, run)


def _metric(reason: str) -> float:
    return REGISTRY.get_sample_value("dashboard_access_denied_total", {"reason": reason}) or 0.0


def _login(client, username: str, password: str, next_path: str = "/dashboard"):
    cookie, form_token = csrf.new_login_token(get_settings().dashboard_csrf_secret.get_secret_value())
    client.cookies.set(csrf.LOGIN_COOKIE_NAME, cookie)
    data = {"username": username, "password": password, "next": next_path, "_csrf": form_token}
    return client.post(LOGIN, data=data, follow_redirects=False)


def _hidden_token(html: str) -> str:
    match = re.search(r'name="_csrf" value="([^"]+)"', html)
    assert match, "no _csrf field rendered"
    return match.group(1)


def _assert_csrf_refused(response) -> None:
    assert response.status_code == 403, (response.status_code, response.text[:300])
    error = response.json()["error"]
    assert error["code"] == "csrf_token_invalid" and error["reason"] == "csrf_token_invalid"


def _reauth_next(response) -> str:
    assert response.status_code == 303, (response.status_code, response.text[:300])
    location = response.headers["location"]
    assert location.startswith(REAUTH + "?next="), location
    return parse_qs(urlsplit(location).query)["next"][0]


# ---------------------------------------------------------------------------
# D24 assumption: a dependency that reads the form first does not break the handler
# ---------------------------------------------------------------------------


def _form_cache_app() -> FastAPI:
    async def read_form_first(request: Request) -> None:
        form = await request.form()
        request.state.seen_token = form.get("_csrf")

    router = APIRouter(dependencies=[Depends(read_form_first)])

    @router.post("/form-params")
    async def form_params(request: Request, name: str = Form(...), count: int = Form(...)):
        return {"name": name, "count": count, "seen": request.state.seen_token}

    @router.post("/request-form")
    async def request_form(request: Request):
        form = await request.form()
        return {"name": form.get("name"), "seen": request.state.seen_token}

    @router.post("/upload")
    async def upload(request: Request, note: str = Form(...), file: UploadFile = File(...)):
        return {"note": note, "content": (await file.read()).decode("utf-8"), "filename": file.filename, "seen": request.state.seen_token}

    @router.post("/upload-request-form")
    async def upload_request_form(request: Request):
        form = await request.form()
        upload_file = form.get("file")
        return {"note": form.get("note"), "content": (await upload_file.read()).decode("utf-8"), "seen": request.state.seen_token}

    test_app = FastAPI()
    test_app.include_router(router)
    return test_app


def test_form_read_by_a_dependency_is_still_available_to_form_parameters_and_request_form():
    with TestClient(_form_cache_app()) as test_client:
        body = test_client.post("/form-params", data={"_csrf": "tok", "name": "alpha", "count": "3"}).json()
        assert body == {"name": "alpha", "count": 3, "seen": "tok"}
        body = test_client.post("/request-form", data={"_csrf": "tok", "name": "beta"}).json()
        assert body == {"name": "beta", "seen": "tok"}


def test_multipart_read_by_a_dependency_is_still_available_to_file_parameters():
    with TestClient(_form_cache_app()) as test_client:
        files = {"file": ("evidence.txt", b"line one\nline two", "text/plain")}
        body = test_client.post("/upload", data={"_csrf": "tok", "note": "n1"}, files=files).json()
        assert body == {"note": "n1", "content": "line one\nline two", "filename": "evidence.txt", "seen": "tok"}
        files = {"file": ("evidence.txt", b"payload", "text/plain")}
        body = test_client.post("/upload-request-form", data={"_csrf": "tok", "note": "n2"}, files=files).json()
        assert body == {"note": "n2", "content": "payload", "seen": "tok"}


def test_real_form_parameter_route_works_with_the_csrf_field(client, signed_in, session_factory):
    """``/dashboard/account/password`` uses ``Form(...)`` parameters after ``csrf_protect`` read the form."""
    who = signed_in(role="operator")
    signed_in.auto_csrf["on"] = False
    new_password = "pw-new-" + who.password
    response = client.post(
        "/dashboard/account/password",
        data={"_csrf": who.csrf, "current_password": who.password, "new_password": new_password, "confirm_password": new_password},
        follow_redirects=False,
    )
    assert response.status_code == 303 and "notice=password_changed" in response.headers["location"]


def test_real_request_form_route_works_with_the_csrf_field(client, signed_in, session_factory):
    """Judge profile create reads ``await request.form()`` in the handler after ``csrf_protect``."""
    who = signed_in(role="admin")
    signed_in.auto_csrf["on"] = False
    response = client.post(
        "/dashboard/judge-profiles",
        data={"_csrf": who.csrf, "name": "csrf-draft", "provider": "openai_compatible"},
        headers={"origin": "http://testserver"},
        follow_redirects=False,
    )
    assert response.status_code == 303 and "csrf-draft" in response.headers["location"] and "error=" not in response.headers["location"]


# ---------------------------------------------------------------------------
# tokens
# ---------------------------------------------------------------------------


def test_session_token_is_bound_to_secret_and_session():
    token = csrf.session_token("secret-a", "ses_1")
    assert token == csrf.session_token("secret-a", "ses_1") and len(token) == 64
    assert token != csrf.session_token("secret-b", "ses_1") and token != csrf.session_token("secret-a", "ses_2")
    assert csrf.verify_session_token("secret-a", "ses_1", token)
    assert not csrf.verify_session_token("secret-a", "ses_1", "") and not csrf.verify_session_token("secret-a", "ses_1", token[:-1] + "0")
    assert csrf.session_token("", "ses_1") == "" and not csrf.verify_session_token(None, "ses_1", token)


def test_login_token_signature_expiry_and_pairing():
    cookie, form_token = csrf.new_login_token("key-1", now=1_000_000)
    assert csrf.verify_login_token(cookie, form_token, "key-1", now=1_000_000 + 10)
    assert not csrf.verify_login_token(cookie, form_token, "key-1", now=1_000_000 + csrf.LOGIN_TOKEN_TTL_SECONDS + 1)
    assert not csrf.verify_login_token(cookie, form_token, "key-2", now=1_000_010)  # another signing key
    other_cookie, other_token = csrf.new_login_token("key-1", now=1_000_000)
    assert not csrf.verify_login_token(cookie, other_token, "key-1", now=1_000_010)  # token of another rendering
    nonce, issued, signature = cookie.split(".")
    assert not csrf.verify_login_token(f"{nonce}.{int(issued) + 5000}.{signature}", form_token, "key-1", now=1_005_000)
    assert not csrf.verify_login_token("garbage", form_token, "key-1") and not csrf.verify_login_token(cookie, "", "key-1")


# ---------------------------------------------------------------------------
# every non-GET Dashboard route refuses a missing or wrong token (D25)
# ---------------------------------------------------------------------------


def test_write_route_inventory_is_complete():
    assert len(GUARDED_WRITES) >= 61  # 61 POST routes in P3 (sign-in included) + reauth, sign-in tested apart
    assert ("POST", REAUTH, "account.self") in WRITE_ROUTES and ("POST", "/dashboard/logout", security.PUBLIC) in WRITE_ROUTES


@pytest.mark.parametrize(("method", "path", "permission"), GUARDED_WRITES, ids=[f"{m} {p}" for m, p, _ in GUARDED_WRITES])
def test_every_write_route_refuses_missing_and_wrong_tokens_without_side_effects(client, signed_in, session_factory, method, path, permission):
    signed_in(role="admin")
    signed_in.auto_csrf["on"] = False
    before = _snapshot(session_factory)
    target = _concrete(path)
    _assert_csrf_refused(client.request(method, target, data={"tenant_id": "t-csrf", "status": "disabled"}, follow_redirects=False))
    _assert_csrf_refused(client.request(method, target, data={"_csrf": "0" * 64, "tenant_id": "t-csrf"}, follow_redirects=False))
    assert _snapshot(session_factory) == before


def test_valid_form_token_passes_and_header_is_accepted(client, signed_in):
    who = signed_in(role="operator")
    signed_in.auto_csrf["on"] = False
    assert client.post("/dashboard/approvals/expire-stale", data={"_csrf": who.csrf}, follow_redirects=False).status_code == 200
    assert client.post("/dashboard/approvals/expire-stale", headers={csrf.HEADER_NAME: who.csrf}, follow_redirects=False).status_code == 200


def test_wrong_form_field_wins_over_a_correct_header(client, signed_in):
    who = signed_in(role="operator")
    response = client.post(
        "/dashboard/approvals/expire-stale", data={"_csrf": "f" * 64}, headers={csrf.HEADER_NAME: who.csrf}, follow_redirects=False
    )
    _assert_csrf_refused(response)


def test_another_sessions_token_is_refused(client, signed_in):
    first = signed_in(role="operator")
    signed_in(role="operator")  # the cookie jar now holds the second session
    signed_in.auto_csrf["on"] = False
    _assert_csrf_refused(client.post("/dashboard/approvals/expire-stale", data={"_csrf": first.csrf}, follow_redirects=False))


def test_token_of_a_signed_out_session_is_refused(client, signed_in, session_factory):
    who = signed_in(role="operator")
    signed_in.auto_csrf["on"] = False
    old_token = who.csrf
    assert client.post("/dashboard/logout", data={"_csrf": old_token}, follow_redirects=False).headers["location"] == LOGIN
    assert _session_row(session_factory, who.session_id).revoked_at is not None
    client.cookies.clear()  # the fixture's cookie has path "/", so the logout deletion (path /dashboard) misses it
    assert _login(client, who.username, who.password).status_code == 303  # a new session with a new secret
    _assert_csrf_refused(client.post("/dashboard/approvals/expire-stale", data={"_csrf": old_token}, follow_redirects=False))
    page = client.get("/dashboard/approvals")
    assert client.post("/dashboard/approvals/expire-stale", data={"_csrf": _hidden_token(page.text)}, follow_redirects=False).status_code == 200


def test_logout_without_token_keeps_the_session(client, signed_in, session_factory):
    who = signed_in(role="reviewer")
    signed_in.auto_csrf["on"] = False
    _assert_csrf_refused(client.post("/dashboard/logout", follow_redirects=False))
    assert _session_row(session_factory, who.session_id).revoked_at is None


def test_csrf_refusal_is_counted_and_recorded(client, signed_in, session_factory):
    who = signed_in(role="operator")
    signed_in.auto_csrf["on"] = False
    before = _metric("csrf_token_invalid")
    _assert_csrf_refused(client.post("/dashboard/approvals/expire-stale", follow_redirects=False))
    _assert_csrf_refused(client.post("/dashboard/approvals/expire-stale", follow_redirects=False))
    assert _metric("csrf_token_invalid") == before + 2
    events = _events(session_factory, "access.denied")
    assert len(events) == 1  # rate limited per session and permission
    details = events[0].details
    assert details["permission"] == "csrf" and details["reason"] == "csrf_token_invalid" and details["session_id"] == who.session_id
    assert events[0].tenant_id == "system" and who.csrf not in str(details)


def test_signed_out_post_still_answers_401_not_403(client, signed_in):
    signed_in(role="operator")
    client.cookies.clear()
    response = client.post("/dashboard/approvals/expire-stale", follow_redirects=False)
    assert response.status_code == 401 and response.json()["error"]["code"] == "dashboard_sign_in_required"
    response = client.post("/dashboard/reauth", data={"password": "x"}, follow_redirects=False)
    assert response.status_code == 401


def test_off_mode_needs_no_token_and_renders_no_field(client):
    assert client.post("/dashboard/approvals/expire-stale", follow_redirects=False).status_code == 200
    page = client.get("/dashboard/approvals")
    assert page.status_code == 200 and 'name="_csrf"' not in page.text


# ---------------------------------------------------------------------------
# templates (D25)
# ---------------------------------------------------------------------------


def _post_forms(text: str) -> list[tuple[str, str]]:
    forms = []
    for match in re.finditer(r"<form\b[^>]*>", text, re.I | re.S):
        if not re.search(r"method\s*=\s*[\"']post[\"']", match.group(0), re.I):
            continue
        end = text.find("</form>", match.end())
        assert end != -1, f"unclosed form: {match.group(0)}"
        forms.append((match.group(0), text[match.end():end]))
    return forms


def test_every_post_form_in_every_template_has_csrf_input():
    missing, total = [], 0
    for path in sorted(TEMPLATES.glob("*.html")):
        for tag, body in _post_forms(path.read_text(encoding="utf-8")):
            total += 1
            if "{{ csrf_input() }}" not in body:
                missing.append(f"{path.name}: {tag[:80]}")
    assert missing == []
    assert total >= 62  # 61 forms before P4 plus reauth.html


def test_template_contract_catches_a_form_without_token():
    assert _post_forms('<form method="post" action="/x"><button>Go</button></form>') == [('<form method="post" action="/x">', "<button>Go</button>")]
    assert _post_forms('<form method="get" action="/x"></form>') == []


def test_rendered_pages_carry_the_session_token(client, signed_in):
    who = signed_in(role="admin")
    for page in ("/dashboard/approvals", "/dashboard/governance", "/dashboard/account", "/dashboard/admin/users"):
        html = client.get(page).text
        tokens = set(re.findall(r'name="_csrf" value="([^"]+)"', html))
        assert tokens == {who.csrf}, page
    assert re.search(r'action="/dashboard/logout">\s*<input type="hidden" name="_csrf"', client.get("/dashboard").text)


# ---------------------------------------------------------------------------
# sign-in form token
# ---------------------------------------------------------------------------


def test_login_page_sets_signed_cookie_and_matching_field(client, signed_in):
    who = signed_in(role="operator")
    client.cookies.clear()
    page = client.get(LOGIN)
    cookie_header = page.headers["set-cookie"]
    assert cookie_header.startswith(csrf.LOGIN_COOKIE_NAME + "=")
    lowered = cookie_header.lower()
    assert "httponly" in lowered and "samesite=strict" in lowered and "path=/dashboard/login" in lowered
    token = _hidden_token(page.text)
    response = client.post(LOGIN, data={"username": who.username, "password": who.password, "next": "/dashboard", "_csrf": token}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/dashboard"
    set_cookies = response.headers.get_list("set-cookie")
    assert any(item.startswith(sessions.COOKIE_NAME + "=") for item in set_cookies)
    assert any(item.startswith(csrf.LOGIN_COOKIE_NAME + "=") and "max-age=0" in item.lower() for item in set_cookies)


def test_login_without_valid_token_is_refused_before_checking_the_password(client, signed_in, session_factory):
    who = signed_in(role="operator")
    client.cookies.clear()
    before = _metric("csrf_token_invalid")
    response = client.post(LOGIN, data={"username": who.username, "password": who.password, "next": "/dashboard/runs"}, follow_redirects=False)
    assert response.status_code == 403 and "The sign-in form expired" in response.text
    assert sessions.COOKIE_NAME + "=" not in response.headers.get("set-cookie", "")
    assert 'name="next" value="/dashboard/runs"' in response.text
    fresh = _hidden_token(response.text)  # the re-rendered form works
    retry = client.post(LOGIN, data={"username": who.username, "password": who.password, "next": "/dashboard/runs", "_csrf": fresh}, follow_redirects=False)
    assert retry.status_code == 303 and retry.headers["location"] == "/dashboard/runs"
    assert _metric("csrf_token_invalid") == before + 1
    assert _user(session_factory, who.user_id).failed_attempts == 0


def test_login_token_from_another_rendering_or_without_cookie_is_refused(client, signed_in):
    who = signed_in(role="operator")
    client.cookies.clear()
    stolen = _hidden_token(client.get(LOGIN).text)
    client.cookies.clear()
    assert client.post(LOGIN, data={"username": who.username, "password": who.password, "_csrf": stolen}, follow_redirects=False).status_code == 403
    client.get(LOGIN)  # a different cookie now
    assert client.post(LOGIN, data={"username": who.username, "password": who.password, "_csrf": stolen}, follow_redirects=False).status_code == 403


def test_expired_login_cookie_is_refused(client, signed_in):
    who = signed_in(role="operator")
    client.cookies.clear()
    key = get_settings().dashboard_csrf_secret.get_secret_value()
    cookie, token = csrf.new_login_token(key, now=time.time() - csrf.LOGIN_TOKEN_TTL_SECONDS - 5)
    client.cookies.set(csrf.LOGIN_COOKIE_NAME, cookie)
    assert client.post(LOGIN, data={"username": who.username, "password": who.password, "_csrf": token}, follow_redirects=False).status_code == 403


def test_configured_csrf_secret_signs_the_login_cookie(client, signed_in, monkeypatch):
    monkeypatch.setenv("DASHBOARD_CSRF_SECRET", "configured-key-for-tests")
    get_settings.cache_clear()
    who = signed_in(role="operator")
    client.cookies.clear()
    cookie, token = csrf.new_login_token("configured-key-for-tests")
    client.cookies.set(csrf.LOGIN_COOKIE_NAME, cookie)
    assert client.post(LOGIN, data={"username": who.username, "password": who.password, "_csrf": token}, follow_redirects=False).status_code == 303
    client.cookies.clear()
    cookie, token = csrf.new_login_token("")  # the per-process key no longer verifies
    client.cookies.set(csrf.LOGIN_COOKIE_NAME, cookie)
    assert client.post(LOGIN, data={"username": who.username, "password": who.password, "_csrf": token}, follow_redirects=False).status_code == 403


# ---------------------------------------------------------------------------
# re-authentication (D26)
# ---------------------------------------------------------------------------


def test_step_up_inventory_matches_the_permission_table():
    declared = {row[2] for row in STEP_UP_ROUTES}
    assert declared == {"users.manage", "judge.profile.write", "api_keys.manage"}
    # handler-level step-up checks (ensure_permission): tenants.disable, pricing.import_apply, evaluation_policy.relax
    assert {"tenants.disable", "pricing.import_apply", "evaluation_policy.relax"} <= permissions.STEP_UP_PERMISSIONS


@pytest.mark.parametrize(("method", "path", "permission"), STEP_UP_ROUTES, ids=[f"{m} {p}" for m, p, _ in STEP_UP_ROUTES])
def test_stale_session_is_sent_to_reauth_and_nothing_runs(client, signed_in, session_factory, method, path, permission):
    who = signed_in(role="admin")
    _age_reauth(session_factory, who.session_id)
    before = _snapshot(session_factory)
    metric = _metric("reauth_required")
    response = client.request(
        method,
        _concrete(path),
        data={"username": "new-user", "role": "operator", "tenant_id": "t-x", "name": "k1"},
        headers={"referer": "http://testserver/dashboard/governance?tenant_id=t-x", "origin": "http://testserver"},
        follow_redirects=False,
    )
    assert _reauth_next(response) == "/dashboard/governance?tenant_id=t-x"
    assert _snapshot(session_factory) == before
    assert _metric("reauth_required") == metric + 1


def test_fresh_sign_in_counts_as_reauthenticated(client, signed_in, session_factory):
    signed_in(role="admin")
    response = client.post(
        "/dashboard/admin/users",
        data={"username": "fresh-created", "role": "operator", "password": "pw-" + "a1b2c3d4e5f6"},
        follow_redirects=False,
    )
    assert response.status_code == 303 and "notice=created" in response.headers["location"]


def test_non_step_up_actions_and_pages_ignore_reauth_age(client, signed_in, session_factory):
    who = signed_in(role="admin")
    _age_reauth(session_factory, who.session_id, seconds=3600)
    assert client.post("/dashboard/approvals/expire-stale", follow_redirects=False).status_code == 200
    assert client.get("/dashboard/admin/users").status_code == 200  # GET of a step-up page needs no re-authentication


def test_reauth_page_renders_with_csrf_field_and_next(client, signed_in):
    who = signed_in(role="reviewer")
    page = client.get(REAUTH + "?next=%2Fdashboard%2Freviews")
    assert page.status_code == 200 and 'data-testid="reauth"' in page.text
    assert _hidden_token(page.text) == who.csrf and 'name="next" value="/dashboard/reviews"' in page.text
    assert "Confirm it&#39;s you" in page.text or "Confirm it's you" in page.text


def test_reauth_success_updates_reauth_at_records_event_and_unlocks_the_action(client, signed_in, session_factory):
    who = signed_in(role="admin")
    _age_reauth(session_factory, who.session_id)
    blocked = client.post("/dashboard/admin/users", data={"username": "later-user", "role": "operator", "password": "pw-" + who.password},
                          headers={"referer": "http://testserver/dashboard/admin/users"}, follow_redirects=False)
    assert _reauth_next(blocked) == "/dashboard/admin/users"
    stale = _session_row(session_factory, who.session_id).reauth_at
    response = client.post(REAUTH, data={"password": who.password, "next": "/dashboard/admin/users"}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/dashboard/admin/users"
    assert _session_row(session_factory, who.session_id).reauth_at.replace(tzinfo=None) > stale.replace(tzinfo=None)
    events = _events(session_factory, "access.reauth")
    assert len(events) == 1 and events[0].details["session_id"] == who.session_id and who.password not in str(events[0].details)
    # the original POST was not replayed; submitting it again now works
    assert not any(user.username == "later-user" for user in _db(session_factory, lambda s: _all_users(s)))
    again = client.post("/dashboard/admin/users", data={"username": "later-user", "role": "operator", "password": "pw-" + who.password}, follow_redirects=False)
    assert "notice=created" in again.headers["location"]


async def _all_users(session):
    return list((await session.scalars(select(DashboardUser))).all())


def test_reauth_failures_count_towards_the_lockout(client, signed_in, session_factory):
    who = signed_in(role="admin")
    _age_reauth(session_factory, who.session_id)
    before = _metric("reauth_failed")
    for attempt in range(5):
        page = client.post(REAUTH, data={"password": f"wrong-{attempt}-{who.password}", "next": "/dashboard"}, follow_redirects=False)
        assert page.status_code == 200 and 'data-testid="reauth-error"' in page.text
    user = _user(session_factory, who.user_id)
    assert user.locked_until is not None  # the 5th failure locks the account (and resets the counter)
    assert _events(session_factory, "access.lockout")
    locked = client.post(REAUTH, data={"password": who.password, "next": "/dashboard"}, follow_redirects=False)
    assert locked.status_code == 200 and 'data-testid="reauth-error"' in locked.text  # even the right password
    assert _metric("reauth_failed") == before + 6
    assert not _events(session_factory, "access.reauth")


@pytest.mark.parametrize("bad_next", ["https://evil.example/x", "//evil.example", "/v1/runs", "/dashboard/login", "/dashboard/reauth?next=/x", ""])
def test_reauth_next_only_accepts_dashboard_paths(client, signed_in, bad_next):
    who = signed_in(role="operator")
    response = client.post(REAUTH, data={"password": who.password, "next": bad_next}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/dashboard"


def test_referer_from_another_host_falls_back_to_dashboard(client, signed_in, session_factory):
    who = signed_in(role="admin")
    _age_reauth(session_factory, who.session_id)
    response = client.post("/dashboard/admin/users", data={"username": "x-user"}, headers={"referer": "https://evil.example/dashboard/runs"}, follow_redirects=False)
    assert _reauth_next(response) == "/dashboard"
    response = client.post("/dashboard/admin/users", data={"username": "x-user"}, follow_redirects=False)
    assert _reauth_next(response) == "/dashboard"


def test_disabling_a_tenant_needs_reauth_but_creating_one_does_not(client, signed_in, session_factory):
    who = signed_in(role="admin")
    _age_reauth(session_factory, who.session_id)
    blocked = client.post("/dashboard/governance/tenants", data={"tenant_id": "t-off", "status": "disabled"}, follow_redirects=False)
    _reauth_next(blocked)
    tenants = _db(session_factory, lambda s: _tenant_ids(s))
    assert "t-off" not in tenants
    created = client.post("/dashboard/governance/tenants", data={"tenant_id": "t-on", "status": "active"}, follow_redirects=False)
    assert created.status_code in {200, 303}
    assert "t-on" in _db(session_factory, lambda s: _tenant_ids(s))


async def _tenant_ids(session):
    return {tenant.tenant_id for tenant in (await session.scalars(select(Tenant))).all()}


def test_pricing_import_apply_needs_reauth(client, signed_in, session_factory):
    who = signed_in(role="admin")
    _age_reauth(session_factory, who.session_id)
    before = _snapshot(session_factory)
    _reauth_next(client.post("/dashboard/admin/finops/pricing/import", data={"mode": "apply"}, follow_redirects=False))
    assert _snapshot(session_factory) == before


def _policy(session_factory, tenant_id: str):
    return _db(session_factory, lambda s: s.scalar(select(TenantEvaluationPolicy).where(TenantEvaluationPolicy.tenant_id == tenant_id)))


def test_relaxing_protected_paths_needs_reauth_tightening_does_not(client, signed_in, session_factory):
    who = signed_in(role="admin")
    _age_reauth(session_factory, who.session_id)
    url = "/dashboard/governance/evaluation-policy"
    added = client.post(url, data={"tenant_id": "t-pol", "protected_paths": "scoring/**", "allow_default_protected_paths": "on"}, follow_redirects=False)
    assert added.status_code == 303 and added.headers["location"].startswith("/dashboard/governance")
    assert _policy(session_factory, "t-pol").protected_paths == ["scoring/**"]
    removed = client.post(url, data={"tenant_id": "t-pol", "protected_paths": "", "allow_default_protected_paths": "on"}, follow_redirects=False)
    _reauth_next(removed)
    defaults_off = client.post(url, data={"tenant_id": "t-pol", "protected_paths": "scoring/**"}, follow_redirects=False)
    _reauth_next(defaults_off)
    policy = _policy(session_factory, "t-pol")
    assert policy.protected_paths == ["scoring/**"] and policy.allow_default_protected_paths is True


def test_tenant_admin_relaxing_own_policy_needs_reauth(client, signed_in, session_factory):
    who = signed_in(role="tenant_admin", tenant_id="t-own")
    url = "/dashboard/governance/evaluation-policy"
    assert client.post(url, data={"tenant_id": "t-own", "protected_paths": "grader/**", "allow_default_protected_paths": "on"}, follow_redirects=False).status_code == 303
    _age_reauth(session_factory, who.session_id)
    _reauth_next(client.post(url, data={"tenant_id": "t-own", "protected_paths": "", "allow_default_protected_paths": "on"}, follow_redirects=False))


def test_off_mode_has_no_step_up(client):
    response = client.post("/dashboard/judge-profiles/deactivate", headers={"origin": "http://testserver"}, follow_redirects=False)
    assert not response.headers.get("location", "").startswith(REAUTH)
    assert client.get(REAUTH + "?next=%2Fdashboard%2Fruns", follow_redirects=False).headers["location"] == "/dashboard/runs"


def test_safe_next_refuses_the_reauth_page_itself():
    assert security.safe_next("/dashboard/reauth?next=/dashboard") == "/dashboard"
    assert security.reauth_redirect_url("/dashboard/costs?tenant_id=a&b=c") == "/dashboard/reauth?next=%2Fdashboard%2Fcosts%3Ftenant_id%3Da%26b%3Dc"


# ---------------------------------------------------------------------------
# security headers (D27)
# ---------------------------------------------------------------------------


def _assert_dashboard_headers(response) -> None:
    for name, value in DASHBOARD_SECURITY_HEADERS.items():
        assert response.headers.get(name) == value, (name, response.headers.get(name))


def test_dashboard_responses_carry_security_headers(client, signed_in):
    _assert_dashboard_headers(client.get("/dashboard"))
    _assert_dashboard_headers(client.get("/dashboard/runs"))
    signed_in(role="auditor")
    _assert_dashboard_headers(client.get("/dashboard/admin/users"))  # 403 page
    _assert_dashboard_headers(client.post("/dashboard/approvals/expire-stale", follow_redirects=False))  # 403 JSON
    client.cookies.clear()
    _assert_dashboard_headers(client.get("/dashboard/runs", follow_redirects=False))  # 303 to sign-in
    _assert_dashboard_headers(client.get(LOGIN))


def test_api_and_root_responses_are_unchanged(client):
    for path in ("/healthz", "/v1/runs"):
        response = client.get(path)
        for name in ("X-Frame-Options", "Content-Security-Policy", "Referrer-Policy"):
            assert name not in response.headers, (path, name)
        assert response.headers.get("Cache-Control") != "no-store"
