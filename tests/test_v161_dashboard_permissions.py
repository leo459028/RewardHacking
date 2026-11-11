"""v1.6.1 P3: Dashboard roles, permissions, tenant scope and operator identity (plan D19-D23, feature six).

Sessions come from the ``signed_in`` fixture (``tests/conftest.py``); the API stays unauthenticated in these tests, so
runs are created through ``/v1/agent/submit`` and then inspected through the Dashboard as a signed-in user.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import get_settings
from app.infra.postgres import get_session
from app.main import app, dashboard_forbidden_handler
from app.models.governance_event import GovernanceEvent
from app.models.tenant import Tenant
from app.observability.metrics import DASHBOARD_ACCESS_DENIED_TOTAL
from app.services.access import permissions
from app.services.review_queue import get_review, list_reviews
from app.ui import security
from app.ui.routes import dashboard_auth
from app.workers.consumer import process_run_by_id
from tests.openapi_contract import iter_routes

FIXTURES = Path("tests/fixtures/v158")
ROLES = permissions.ROLES


@pytest.fixture(autouse=True)
def _fresh_denial_log():
    security.reset_denial_log()
    yield
    security.reset_denial_log()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _guarded_routes() -> list[tuple[str, str, str]]:
    """``(method, path, declared permission)`` for every Dashboard route (plus ``/`` and the favicon)."""
    rows = []
    for route in iter_routes(app.routes):
        path = getattr(route, "path", "")
        if not (path.startswith("/dashboard") or path in {"/", "/favicon.ico"}):
            continue
        for method in sorted(getattr(route, "methods", set()) - {"HEAD"}):
            rows.append((method, path, security.declared_permission(route)))
    return sorted(rows)


ROUTES = _guarded_routes()
POST_ROUTES = [row for row in ROUTES if row[0] == "POST" and row[2] not in {security.PUBLIC, "account.self"}]


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "zz-placeholder", path)


def _db(session_factory, fn):
    async def run():
        async with session_factory() as session:
            return await fn(session)

    return asyncio.run(run())


async def _scalars(session, stmt):
    return list((await session.scalars(stmt)).all())


def _denied_events(session_factory) -> list[GovernanceEvent]:
    return _db(session_factory, lambda s: _scalars(s, select(GovernanceEvent).where(GovernanceEvent.event_type == "access.denied")))


def _submit(client, tenant: str, *, goal: str = "Summarize the anomaly causes", task_type: str = "analysis") -> str:
    payload = {
        "tenant_id": tenant,
        "user_id": "agent-1",
        "task_type": task_type,
        "prompt": "Analyze the provided dataset carefully.",
        "goal": goal,
        "allowed_tools": ["read_data"],
        "constraints": {"max_tool_calls": 2},
    }
    body = client.post("/v1/agent/submit", json=payload).json()
    assert body["status"] == "queued", body
    return body["run_id"]


def _run_worker(client, session_factory, run_id: str) -> None:
    _db(
        session_factory,
        lambda s: process_run_by_id(s, run_id=run_id, state_store=client.state_store, event_publisher=client.event_publisher),
    )


def _completed_code_run(client, session_factory, tenant: str) -> str:
    payload = {
        "tenant_id": tenant,
        "user_id": "agent-1",
        "task_type": "code_fix",
        "prompt": "Fix the failing date parser test.",
        "goal": "Fix the failing date parser",
        "allowed_tools": ["read_repo", "run_tests"],
        "constraints": {"max_tool_calls": 2},
    }
    run_id = client.post("/v1/agent/submit", json=payload).json()["run_id"]
    _run_worker(client, session_factory, run_id)
    return run_id


def _review_run(client, session_factory, tenant: str) -> tuple[str, str]:
    """A ``review_required`` run (legacy ``bypass`` signal) and its open review."""
    run_id = _submit(client, tenant, goal="Bypass the stale cache entries")
    _run_worker(client, session_factory, run_id)
    reviews = _db(session_factory, lambda s: list_reviews(s, run_id=run_id))
    assert reviews, "expected a review"
    return run_id, reviews[0].review_id


def _outcome_form(**extra) -> dict:
    form = {
        "provenance": "self_reported",
        "tests_passed": "12",
        "tests_failed": "0",
        "test_format": "summary",
        "final_message": "All tests pass.",
        "diff": (FIXTURES / "grader_tampering_eq_true.diff").read_text(encoding="utf-8"),
    }
    form.update(extra)
    return form


def _assert_forbidden_page(response, permission: str, reason: str = "missing_permission") -> None:
    assert response.status_code == 403, response.text[:300]
    assert 'data-testid="forbidden"' in response.text
    assert f'data-testid="forbidden-permission">{permission}<' in response.text
    assert f'data-testid="forbidden-reason">{reason}<' in response.text


def _assert_forbidden_json(response, permission: str, code: str = "permission_denied") -> None:
    assert response.status_code == 403, response.text[:300]
    error = response.json()["error"]
    assert error["code"] == code and error["permission"] == permission


# ---------------------------------------------------------------------------
# contract: every Dashboard route declares a permission (D19)
# ---------------------------------------------------------------------------


def test_every_dashboard_route_declares_a_known_permission():
    assert len(ROUTES) > 80
    undeclared = [(method, path) for method, path, perm in ROUTES if perm is None]
    assert undeclared == []
    unknown = [(method, path, perm) for method, path, perm in ROUTES if perm != security.PUBLIC and perm not in permissions.PERMISSIONS]
    assert unknown == []
    public = sorted((method, path) for method, path, perm in ROUTES if perm == security.PUBLIC)
    assert public == [("GET", "/dashboard/login"), ("POST", "/dashboard/login"), ("POST", "/dashboard/logout")]


def test_require_permission_rejects_unknown_names():
    with pytest.raises(ValueError):
        security.require_permission("no.such.permission")


def test_undeclared_route_is_refused_at_run_time(session_factory):
    probe = FastAPI()
    router = APIRouter(dependencies=[Depends(dashboard_auth)])

    @router.get("/dashboard/undeclared-probe")
    async def undeclared_probe():  # pragma: no cover - must never run
        raise AssertionError("an undeclared route ran")

    @router.post("/dashboard/undeclared-probe")
    async def undeclared_probe_post():  # pragma: no cover - must never run
        raise AssertionError("an undeclared route ran")

    async def override_session():
        async with session_factory() as session:
            yield session

    probe.include_router(router)
    probe.add_exception_handler(security.DashboardForbidden, dashboard_forbidden_handler)
    probe.dependency_overrides[get_session] = override_session
    get_settings.cache_clear()
    with TestClient(probe) as raw:
        page = raw.get("/dashboard/undeclared-probe")
        posted = raw.post("/dashboard/undeclared-probe")
    _assert_forbidden_page(page, "undeclared", "permission_undeclared")
    _assert_forbidden_json(posted, "undeclared", "permission_undeclared")


def test_permission_table_follows_api_roles():
    """D20: where the API is narrower the Dashboard follows it (P3 progress log lists every difference)."""
    expect = {
        "runs.submit": {"admin", "operator"},  # /v1/agent/submit
        "approvals.decide": {"admin", "operator"},  # /v1/approvals/{id}/decision
        "approvals.expire": {"admin", "operator"},
        "outcomes.submit": {"admin", "operator"},
        "reviews.decide": {"admin", "tenant_admin", "reviewer"},
        "reviews.retriage": {"admin", "tenant_admin"},
        "breakers.manage": {"admin"},
        "benchmark.import": {"admin"},
        "honeypots.package": {"admin", "tenant_admin"},
        "operations.manage": {"admin"},
        "retention.plan": {"admin"},
    }
    for name, roles in expect.items():
        assert set(permissions.PERMISSIONS[name]) == roles, name
    assert "operator" not in permissions.PERMISSIONS["benchmark.view"]
    assert "benchmark" not in permissions.TENANT_VIEW_ALLOWLIST and "budgets" in permissions.TENANT_VIEW_ALLOWLIST


# ---------------------------------------------------------------------------
# role x route matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", [role for role in ROLES if role != "admin"])
def test_role_route_matrix_denies_exactly_what_the_table_denies(client, signed_in, session_factory, role):
    """Every route the role's permissions do not cover answers 403 (GET: page naming the permission; others: JSON).
    Allowed routes are checked statically here and exercised by the flow tests below."""
    tenant = "t-matrix" if role == "tenant_admin" else None
    signed_in(role=role, tenant_id=tenant)
    denied = [(m, p, perm) for m, p, perm in ROUTES if perm not in {security.PUBLIC} and not permissions.has_permission(role, perm)]
    assert denied, role
    for method, path, perm in denied:
        response = client.request(method, _concrete(path), follow_redirects=False)
        if method == "GET":
            _assert_forbidden_page(response, perm)
        else:
            _assert_forbidden_json(response, perm)


def test_admin_holds_every_declared_permission(client, signed_in):
    signed_in(role="admin")
    for _method, _path, perm in ROUTES:
        assert perm == security.PUBLIC or permissions.has_permission("admin", perm)
    assert client.get("/dashboard/admin/users").status_code == 200
    assert client.get("/dashboard/control-plane").status_code == 200


@pytest.mark.parametrize(("method", "path", "perm"), POST_ROUTES, ids=[f"{m} {p}" for m, p, _ in POST_ROUTES])
def test_auditor_is_refused_every_post_route(client, signed_in, method, path, perm):
    signed_in(role="auditor")
    response = client.post(_concrete(path), data={"tenant_id": "default", "reason": "audit probe"}, follow_redirects=False)
    _assert_forbidden_json(response, perm)


def test_auditor_post_refusals_have_no_side_effects(client, signed_in, session_factory):
    signed_in(role="auditor")
    client.post("/dashboard/governance/tenants", data={"tenant_id": "auditor-made"})
    client.post("/dashboard/control-plane/tools/read_data/breaker/open", data={"reason": "x"})
    tenants = _db(session_factory, lambda s: _scalars(s, select(Tenant).where(Tenant.tenant_id == "auditor-made")))
    assert tenants == []
    events = _db(session_factory, lambda s: _scalars(s, select(GovernanceEvent).where(GovernanceEvent.event_type == "tool_breaker.opened")))
    assert events == []
    assert len(POST_ROUTES) >= 55  # recorded in the P3 progress log


def test_auditor_reads_every_view_but_users(client, signed_in):
    signed_in(role="auditor")
    for path in ("/dashboard", "/dashboard/runs", "/dashboard/control-plane", "/dashboard/worker", "/dashboard/governance",
                 "/dashboard/costs", "/dashboard/admin/finops/pricing", "/dashboard/operations", "/dashboard/observability",
                 "/dashboard/retention", "/dashboard/judge-profiles", "/dashboard/benchmark", "/dashboard/reviews"):
        assert client.get(path).status_code == 200, path
    _assert_forbidden_page(client.get("/dashboard/admin/users"), "users.manage")
    export = client.get("/dashboard/costs/export?tenant_id=default&format=json")
    assert export.status_code == 200


def test_operator_cannot_open_benchmark_or_honeypot_package(client, signed_in):
    signed_in(role="operator")
    _assert_forbidden_page(client.get("/dashboard/benchmark"), "benchmark.view")
    _assert_forbidden_page(client.get("/dashboard/honeypots/hp-x/package"), "honeypots.package")
    _assert_forbidden_page(client.get("/dashboard/costs/export"), "finops.export")


def test_navigation_and_buttons_follow_permissions(client, signed_in):
    signed_in(role="auditor")
    overview = client.get("/dashboard").text
    assert 'href="/dashboard/playground"' not in overview and 'href="/dashboard/admin/users"' not in overview
    assert 'href="/dashboard/retention"' in overview and 'href="/dashboard/benchmark"' in overview
    approvals = client.get("/dashboard/approvals").text
    assert 'action="/dashboard/approvals/expire-stale"' not in approvals
    control = client.get("/dashboard/control-plane").text
    assert 'action="/dashboard/control-plane/tools"' not in control

    signed_in(role="admin")
    overview = client.get("/dashboard").text
    assert 'href="/dashboard/admin/users"' in overview and 'href="/dashboard/playground"' in overview
    assert 'action="/dashboard/approvals/expire-stale"' in client.get("/dashboard/approvals").text


def test_off_mode_local_admin_keeps_every_button(client):
    page = client.get("/dashboard/approvals").text
    assert 'action="/dashboard/approvals/expire-stale"' in page
    assert 'action="/dashboard/control-plane/tools"' in client.get("/dashboard/control-plane").text


# ---------------------------------------------------------------------------
# access.denied events and metrics (D18)
# ---------------------------------------------------------------------------


def test_denial_writes_one_access_denied_event_per_minute_and_counts_every_refusal(client, signed_in, session_factory):
    who = signed_in(role="reviewer")
    counter = DASHBOARD_ACCESS_DENIED_TOTAL.labels(reason="missing_permission")
    before = counter._value.get()
    for _ in range(3):
        assert client.get("/dashboard/admin/users").status_code == 403
    assert client.post("/dashboard/approvals/expire-stale").status_code == 403
    assert counter._value.get() - before == 4
    events = _denied_events(session_factory)
    by_permission = {event.details["permission"]: event for event in events}
    assert len(events) == 2 and set(by_permission) == {"users.manage", "approvals.expire"}
    event = by_permission["users.manage"]
    assert event.tenant_id == "system" and event.user_id == f"user:{who.username}" and event.decision == "deny"
    assert event.details["reason"] == "missing_permission" and event.details["session_id"] == who.session_id
    assert event.details["path"] == "/dashboard/admin/users" and event.details["role"] == "reviewer"


def test_tenant_scope_refusal_is_counted_separately(client, signed_in):
    signed_in(role="reviewer", tenant_id="t-a")
    counter = DASHBOARD_ACCESS_DENIED_TOTAL.labels(reason="tenant_scope")
    platform = DASHBOARD_ACCESS_DENIED_TOTAL.labels(reason="platform_view")
    before, platform_before = counter._value.get(), platform._value.get()
    assert client.get("/dashboard/runs?tenant_id=t-b").status_code == 403
    assert client.get("/dashboard/control-plane").status_code == 403
    assert counter._value.get() - before == 1 and platform._value.get() - platform_before == 1


# ---------------------------------------------------------------------------
# tenant scope (D22)
# ---------------------------------------------------------------------------


def test_tenant_bound_lists_are_forced_to_the_own_tenant(client, signed_in):
    own = _submit(client, "t-a")
    other = _submit(client, "t-b")
    signed_in(role="reviewer", tenant_id="t-a")
    for path in ("/dashboard", "/dashboard/runs", "/dashboard/approvals", "/dashboard/governance"):
        page = client.get(path)
        assert page.status_code == 200, path
        assert other not in page.text, path
    runs = client.get("/dashboard/runs").text
    assert own in runs and other not in runs
    assert own in client.get("/dashboard/runs?tenant_id=t-a").text
    response = client.get("/dashboard/runs?tenant_id=t-b")
    _assert_forbidden_page(response, "dashboard.view", "tenant_scope")
    assert other not in response.text
    _assert_forbidden_page(client.get("/dashboard/governance?tenant_id=t-b"), "dashboard.view", "tenant_scope")
    _assert_forbidden_page(client.get("/dashboard/costs?tenant_id=t-b"), "dashboard.view", "tenant_scope")
    assert client.get("/dashboard/costs").status_code == 200


def test_tenant_bound_governance_shows_only_own_tenant_events(client, signed_in, session_factory):
    signed_in(role="admin")
    client.post("/dashboard/governance/tenants", data={"tenant_id": "t-a"})
    client.post("/dashboard/governance/tenants", data={"tenant_id": "t-b", "display_name": "Bravo Secret Corp"})
    signed_in(role="tenant_admin", tenant_id="t-a")
    page = client.get("/dashboard/governance").text
    assert "Bravo Secret Corp" not in page and "Tenant t-b created" not in page
    assert "Tenant t-a created" in page


def test_cross_tenant_run_and_review_urls_answer_403_without_content(client, signed_in, session_factory):
    other_run, other_review = _review_run(client, session_factory, "t-b")
    own_run, own_review = _review_run(client, session_factory, "t-a")
    signed_in(role="reviewer", tenant_id="t-a")
    run_page = client.get(f"/dashboard/runs/{other_run}")
    _assert_forbidden_page(run_page, "dashboard.view", "tenant_scope")
    assert "Bypass the stale cache entries" not in run_page.text
    review_page = client.get(f"/dashboard/reviews/{other_review}")
    _assert_forbidden_page(review_page, "dashboard.view", "tenant_scope")
    assert other_run not in review_page.text
    # no existence oracle: an unknown id is refused the same way for tenant-bound users
    _assert_forbidden_page(client.get("/dashboard/runs/run_does_not_exist"), "dashboard.view", "tenant_scope")
    _assert_forbidden_page(client.get("/dashboard/reviews/review_does_not_exist"), "dashboard.view", "tenant_scope")
    assert client.get(f"/dashboard/runs/{own_run}").status_code == 200
    assert client.get(f"/dashboard/reviews/{own_review}").status_code == 200
    queue = client.get("/dashboard/reviews").text
    assert own_review in queue and other_review not in queue
    _assert_forbidden_page(client.get("/dashboard/reviews?tenant_id=t-b"), "dashboard.view", "tenant_scope")

    decided = client.post(f"/dashboard/reviews/{other_review}/decision", data={"label": "accepted", "reason": "x"})
    _assert_forbidden_json(decided, "reviews.decide", "tenant_scope_denied")
    review = _db(session_factory, lambda s: get_review(s, other_review))
    assert review.status == "open"


@pytest.mark.parametrize(
    ("role", "path", "perm"),
    [
        ("reviewer", "/dashboard/control-plane", "dashboard.view"),
        ("reviewer", "/dashboard/worker", "dashboard.view"),
        ("reviewer", "/dashboard/operations", "dashboard.view"),
        ("reviewer", "/dashboard/observability", "dashboard.view"),
        ("reviewer", "/dashboard/admin/finops/pricing", "dashboard.view"),
        ("reviewer", "/dashboard/judge-profiles", "judge.view"),
        ("reviewer", "/dashboard/benchmark", "benchmark.view"),
        ("operator", "/dashboard/retention", "retention.view"),
        ("tenant_admin", "/dashboard/benchmark", "benchmark.view"),
        ("tenant_admin", "/dashboard/judge-profiles", "judge.view"),
        ("auditor", "/dashboard/control-plane", "dashboard.view"),
    ],
)
def test_platform_views_are_refused_to_tenant_bound_users(client, signed_in, role, path, perm):
    signed_in(role=role, tenant_id="t-a")
    _assert_forbidden_page(client.get(path), perm, "platform_view")


def test_tenant_bound_navigation_hides_platform_views(client, signed_in):
    signed_in(role="tenant_admin", tenant_id="t-a")
    page = client.get("/dashboard").text
    for href in ("/dashboard/control-plane", "/dashboard/worker", "/dashboard/operations", "/dashboard/retention",
                 "/dashboard/judge-profiles", "/dashboard/benchmark", "/dashboard/admin/finops/pricing"):
        assert f'href="{href}"' not in page, href
    assert 'href="/dashboard/admin/finops/budgets"' in page and 'href="/dashboard/reviews"' in page


def test_tenant_admin_finops_and_policy_actions_stay_in_own_tenant(client, signed_in):
    signed_in(role="tenant_admin", tenant_id="t-a")
    response = client.post("/dashboard/admin/finops/budgets", data={"tenant_id": "t-b", "hard_limit_amount": "5"})
    _assert_forbidden_json(response, "finops.manage", "tenant_scope_denied")
    created = client.post("/dashboard/admin/finops/budgets", data={"name": "own", "hard_limit_amount": "5"}, follow_redirects=False)
    assert created.status_code == 303
    budgets = client.get("/v1/budgets?tenant_id=t-a").json()
    assert any(item["tenant_id"] == "t-a" for item in budgets)
    _assert_forbidden_page(client.get("/dashboard/admin/finops/budgets?tenant_id=t-b"), "dashboard.view", "tenant_scope")

    policy = client.post("/dashboard/governance/evaluation-policy", data={"tenant_id": "t-b", "protected_paths": "grader/"})
    _assert_forbidden_json(policy, "evaluation_policy.write", "tenant_scope_denied")
    saved = client.post("/dashboard/governance/evaluation-policy", data={"protected_paths": "grader/"}, follow_redirects=False)
    assert saved.status_code == 303 and "tenant_id=t-a" in saved.headers["location"]
    assert "grader/" in client.get("/v1/tenants/t-a/evaluation-policy").json()["protected_paths"]


def test_tenant_admin_cannot_touch_another_tenants_budget(client, signed_in):
    signed_in(role="admin")
    client.post("/dashboard/admin/finops/budgets", data={"tenant_id": "t-b", "name": "theirs", "hard_limit_amount": "5"})
    budget_id = client.get("/v1/budgets?tenant_id=t-b").json()[0]["budget_id"]
    signed_in(role="tenant_admin", tenant_id="t-a")
    response = client.post(f"/dashboard/admin/finops/budgets/{budget_id}/disable")
    _assert_forbidden_json(response, "finops.manage", "tenant_scope_denied")
    assert client.get(f"/v1/budgets/{budget_id}").json()["is_active"] is True


def test_tenant_bound_operator_submits_and_decides_only_in_own_tenant(client, signed_in, session_factory):
    signed_in(role="operator", tenant_id="t-a")
    response = client.post("/dashboard/playground", data={"tenant_id": "t-b", "prompt": "Analyze data.", "goal": "Summarize"})
    _assert_forbidden_json(response, "runs.submit", "tenant_scope_denied")
    page = client.post("/dashboard/playground", data={"prompt": "Analyze the provided dataset.", "goal": "Summarize", "task_type": "analysis",
                                                      "allowed_tools": "read_data", "max_tool_calls": "2"})
    assert page.status_code == 200
    runs = client.get("/v1/runs?tenant_id=t-a").json()
    assert runs and all(run["tenant_id"] == "t-a" for run in runs)

    other = _submit(client, "t-b")
    outcome = client.post(f"/dashboard/runs/{other}/outcome", data=_outcome_form())
    _assert_forbidden_json(outcome, "outcomes.submit", "tenant_scope_denied")
    replay = client.post(f"/dashboard/runs/{other}/replay")
    _assert_forbidden_json(replay, "runs.replay", "tenant_scope_denied")
    decision = client.post(f"/dashboard/approvals/{other}/decision", data={"decision": "deny"})
    _assert_forbidden_json(decision, "approvals.decide", "tenant_scope_denied")
    # platform-wide actions are refused to tenant-bound operators
    _assert_forbidden_json(client.post("/dashboard/approvals/expire-stale"), "approvals.expire", "platform_view_denied")
    _assert_forbidden_json(client.post("/dashboard/worker/dlq/x/requeue"), "worker.manage", "platform_view_denied")


# ---------------------------------------------------------------------------
# operator identity (D23)
# ---------------------------------------------------------------------------


def test_session_mode_records_user_actor_in_governance_events(client, signed_in, session_factory):
    who = signed_in(role="admin")
    assert client.post("/dashboard/control-plane/tools/read_data/breaker/open", data={"reason": "drill"}, follow_redirects=False).status_code == 303
    client.post("/dashboard/governance/tenants", data={"tenant_id": "t-actor"})
    events = _db(session_factory, lambda s: _scalars(s, select(GovernanceEvent).where(GovernanceEvent.event_type.in_(("tool_breaker.opened", "tenant.created")))))
    assert {event.user_id for event in events} == {f"user:{who.username}"}


def test_off_mode_keeps_the_historical_actor_strings(client, session_factory):
    client.post("/dashboard/control-plane/tools/read_data/breaker/open", data={"reason": "drill"})
    events = _db(session_factory, lambda s: _scalars(s, select(GovernanceEvent).where(GovernanceEvent.event_type == "tool_breaker.opened")))
    assert [event.user_id for event in events] == ["dashboard-admin"]


def test_forged_reviewer_field_is_ignored(client, signed_in, session_factory):
    _run_id, review_id = _review_run(client, session_factory, "t-forge")
    who = signed_in(role="reviewer")
    page = client.get(f"/dashboard/reviews/{review_id}").text
    assert 'name="reviewer"' not in page and f"user:{who.username}" in page and 'data-testid="acting-as"' in page
    decided = client.post(
        f"/dashboard/reviews/{review_id}/decision",
        data={"label": "accepted", "reviewer": "someone-else", "reason": "looks fine"},
        follow_redirects=False,
    )
    assert decided.status_code == 303 and "error=" not in decided.headers["location"]
    review = _db(session_factory, lambda s: get_review(s, review_id))
    assert review.reviewer == f"user:{who.username}"


def test_same_user_cannot_review_their_own_outcome(client, signed_in, session_factory):
    run_id = _completed_code_run(client, session_factory, "t-self")
    who = signed_in(role="admin")
    run_page = client.get(f"/dashboard/runs/{run_id}").text
    assert 'name="submitted_by"' not in run_page
    submitted = client.post(f"/dashboard/runs/{run_id}/outcome", data=_outcome_form(submitted_by="ci-bot"), follow_redirects=False)
    assert submitted.status_code == 303 and "message=" in submitted.headers["location"]
    review_id = client.get(f"/v1/reviews?run_id={run_id}").json()[0]["review_id"]
    decided = client.post(
        f"/dashboard/reviews/{review_id}/decision",
        data={"label": "accepted", "reviewer": "a-different-name", "reason": "mine"},
        follow_redirects=False,
    )
    assert decided.status_code == 303 and "error=" in decided.headers["location"]
    review = _db(session_factory, lambda s: get_review(s, review_id))
    assert review.status == "open"
    outcomes = client.get(f"/v1/runs/{run_id}/outcomes").json()
    assert outcomes[-1]["submitted_by"] == f"user:{who.username}"

    signed_in(role="reviewer")
    ok = client.post(f"/dashboard/reviews/{review_id}/decision", data={"label": "confirmed_hacking", "reason": "eq True"}, follow_redirects=False)
    assert ok.status_code == 303 and "error=" not in ok.headers["location"]


def test_session_mode_templates_drop_self_filled_operator_fields(client, signed_in):
    signed_in(role="admin")
    assert 'name="updated_by"' not in client.get("/dashboard/governance").text
    assert 'name="created_by"' not in client.get("/dashboard/operations").text
    assert 'name="reviewer"' not in client.get("/dashboard/approvals").text
    assert 'name="updated_by"' not in client.get("/dashboard/benchmark").text


def test_off_mode_templates_keep_the_operator_fields(client):
    assert 'name="updated_by"' in client.get("/dashboard/governance").text
    assert 'name="created_by"' in client.get("/dashboard/operations").text


def test_legal_hold_from_dashboard_uses_the_signed_in_role(client, signed_in):
    who = signed_in(role="admin")
    created = client.post(
        "/dashboard/retention/holds",
        data={"scope_type": "tenant", "scope_id": "t-hold", "reason": "litigation"},
        headers={"origin": "http://testserver"},
        follow_redirects=False,
    )
    assert created.status_code == 303 and "message=" in created.headers["location"]
    holds = client.get("/v1/legal-holds").json()
    assert any(hold["created_by"] == f"user:{who.username}" for hold in holds)


# ---------------------------------------------------------------------------
# API: auditor key role and GET /v1/governance/events (D21, Gap 16)
# ---------------------------------------------------------------------------


@pytest.fixture
def api_keys(client, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_KEYS", "k-admin:t-api:admin,k-aud:t-api:auditor,k-user:t-api:end_user,k-view:t-api:viewer")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_api_auditor_reads_like_viewer_and_never_writes(client, api_keys):
    aud = {"X-API-Key": "k-aud"}
    assert client.get("/v1/governance/events", headers=aud).status_code == 200
    assert client.get("/v1/approvals", headers=aud).status_code == 200
    assert client.get("/v1/reviews", headers=aud).status_code == 200
    submit = client.post("/v1/agent/submit", headers=aud, json={"user_id": "u", "task_type": "analysis", "prompt": "x", "goal": "y"})
    assert submit.status_code == 403 and submit.json()["error"]["code"] == "insufficient_role"
    expire = client.post("/v1/approvals/expire-stale", headers=aud)
    assert expire.status_code == 403
    policy = client.put("/v1/tenants/t-api/evaluation-policy", headers=aud, json={"protected_paths": []})
    assert policy.status_code == 403


def test_governance_events_endpoint_requires_a_reader_role(client, api_keys):
    assert client.get("/v1/governance/events", headers={"X-API-Key": "k-view"}).status_code == 200
    refused = client.get("/v1/governance/events", headers={"X-API-Key": "k-user"})
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "insufficient_role"
    assert client.get("/v1/governance/events").status_code == 401


def test_api_and_dashboard_can_issue_auditor_keys(client, api_keys):
    admin = {"X-API-Key": "k-admin"}
    assert client.post("/v1/tenants", headers=admin, json={"tenant_id": "t-api", "display_name": "API"}).status_code in {200, 201}
    issued = client.post("/v1/tenants/t-api/api-keys", headers=admin, json={"name": "audit", "role": "auditor"})
    assert issued.status_code == 200 and issued.json()["role"] == "auditor"
    plaintext = issued.json()["plaintext_key"]
    assert client.get("/v1/governance/events", headers={"X-API-Key": plaintext}).status_code == 200


def test_dashboard_issues_auditor_key_role(client):
    page = client.post("/dashboard/governance/api-keys", data={"tenant_id": "t-keys", "name": "auditors", "role": "auditor"})
    assert page.status_code == 200 and '<option value="auditor">auditor</option>' in page.text
    keys = client.get("/v1/tenants/t-keys/api-keys").json()
    assert [key["role"] for key in keys] == ["auditor"]
