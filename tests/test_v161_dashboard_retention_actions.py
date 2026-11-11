"""v1.6.1 P7: Dashboard retention apply and legal hold release (plan D44, feature ten).

``session`` mode only: a signed-in admin sees an apply form (type the ``purge_id`` to confirm) and a release form
(reason required) on ``/dashboard/retention``; other roles and ``off`` mode see none and their POSTs are refused.
Both forms go through ``change_requests.execute_or_request()`` (D39), so with ``DUAL_CONTROL_ACTIONS`` they store a
change request that a second admin approves on ``/dashboard/change-requests``. The service still refuses to delete
unless ``AUDIT_RETENTION_PURGE_MODE=apply``.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote_plus

import pytest
from sqlalchemy import func, select, update

from app.core.config import get_settings
from app.models.access import ChangeRequest, DashboardSession
from app.models.agent_run import AgentRun
from app.models.governance_event import GovernanceEvent
from app.models.retention import LegalHold, RetentionPurge
from app.services import audit_chain
from app.ui.security import reset_denial_log
from tests.test_v160_retention import _add_run

PAGE = "/dashboard/retention"
SAME_ORIGIN = {"Origin": "http://testserver"}


def run(coro):
    return asyncio.run(coro)


def _db(session_factory, fn):
    async def inner():
        async with session_factory() as session:
            return await fn(session)

    return run(inner())


async def _all(session, stmt):
    return list((await session.scalars(stmt)).all())


def _mode(monkeypatch, mode: str = "apply", dual: str = "") -> None:
    monkeypatch.setenv("AUDIT_RETENTION_PURGE_MODE", mode)
    monkeypatch.setenv("DUAL_CONTROL_ACTIONS", dual)
    get_settings.cache_clear()


def _seed_old_run(session_factory, run_id: str = "run_p7_old") -> None:
    async def seed(session):
        _add_run(session, run_id)
        await session.commit()

    _db(session_factory, seed)


def _runs(session_factory) -> int:
    return _db(session_factory, lambda s: s.scalar(select(func.count()).select_from(AgentRun)))


def _plan(client, session_factory) -> dict:
    _seed_old_run(session_factory)
    plan = client.post("/v1/retention/plans").json()
    assert plan["status"] == "planned" and plan["candidate_run_ids"] == ["run_p7_old"]
    return plan


def _hold(client, tenant: str = "t-p7") -> dict:
    response = client.post("/v1/legal-holds", json={"scope_type": "tenant", "scope_id": tenant, "reason": "litigation QA"})
    assert response.status_code == 200
    return response.json()


def _purge(session_factory, purge_id: str) -> RetentionPurge:
    return _db(session_factory, lambda s: s.scalar(select(RetentionPurge).where(RetentionPurge.purge_id == purge_id)))


def _hold_row(session_factory, hold_id: str) -> LegalHold:
    return _db(session_factory, lambda s: s.scalar(select(LegalHold).where(LegalHold.hold_id == hold_id)))


def _crs(session_factory) -> list[ChangeRequest]:
    return _db(session_factory, lambda s: _all(s, select(ChangeRequest).order_by(ChangeRequest.id)))


def _apply(client, plan: dict, *, confirm: str | None = None, digest: str | None = None, headers=None, **extra):
    data = {"confirm_purge_id": plan["purge_id"] if confirm is None else confirm, "plan_digest": digest or plan["plan_digest"], **extra}
    return client.post(
        f"{PAGE}/plans/{plan['purge_id']}/apply", data=data, headers=SAME_ORIGIN if headers is None else headers, follow_redirects=False
    )


def _release(client, hold_id: str, reason: str = "matter closed", headers=None, **extra):
    return client.post(
        f"{PAGE}/holds/{hold_id}/release",
        data={"release_reason": reason, **extra},
        headers=SAME_ORIGIN if headers is None else headers,
        follow_redirects=False,
    )


def _location(response) -> str:
    assert response.status_code == 303, response.text
    return unquote_plus(response.headers["location"])


def _forms(html: str) -> list[str]:
    return re.findall(r'<form[^>]*action="([^"]+)"', html)


def _age_reauth(session_factory, session_id: str) -> None:
    async def age(session):
        await session.execute(
            update(DashboardSession)
            .where(DashboardSession.session_id == session_id)
            .values(reauth_at=datetime.now(timezone.utc) - timedelta(hours=1))
        )
        await session.commit()

    _db(session_factory, age)


# ---------------------------------------------------------------------------
# who sees the forms (D44)
# ---------------------------------------------------------------------------


def test_admin_sees_apply_and_release_forms_instead_of_the_api_command(client, session_factory, signed_in, monkeypatch):
    signed_in(role="admin")
    _mode(monkeypatch, "plan")
    plan = _plan(client, session_factory)
    hold = _hold(client)
    html = client.get(f"{PAGE}?purge_id={plan['purge_id']}").text
    forms = _forms(html)
    assert f"{PAGE}/plans/{plan['purge_id']}/apply" in forms and f"{PAGE}/holds/{hold['hold_id']}/release" in forms
    assert 'id="apply-form"' in html and 'name="confirm_purge_id"' in html and f'value="{plan["plan_digest"]}"' in html
    assert 'id="apply-api-only"' not in html and "curl -X POST" not in html
    assert "Apply purge plan" in html and "Submit change request" not in html
    assert "Use the API (admin required)" not in html
    # every POST form carries the CSRF field (D24)
    assert html.count('name="_csrf"') == html.count('method="post"')


@pytest.mark.parametrize("role", ["operator", "auditor"])
def test_other_roles_see_no_apply_or_release_form(client, session_factory, signed_in, monkeypatch, role):
    _mode(monkeypatch, "apply")
    plan = _plan(client, session_factory)
    _hold(client)
    signed_in(role=role)
    html = client.get(f"{PAGE}?purge_id={plan['purge_id']}").text
    assert not [action for action in _forms(html) if action.endswith("/apply") or action.endswith("/release")]
    assert 'id="apply-needs-permission"' in html and "Needs legal_hold.release (admin)" in html
    assert "curl -X POST" not in html


def test_off_mode_keeps_the_api_command_and_shows_no_forms(client, session_factory, monkeypatch):
    _mode(monkeypatch, "apply")
    plan = _plan(client, session_factory)
    _hold(client)
    html = client.get(f"{PAGE}?purge_id={plan['purge_id']}").text
    assert 'id="apply-api-only"' in html and f"/v1/retention/plans/{plan['purge_id']}/apply" in html
    assert "Use the API (admin required)" in html
    assert _forms(html) == ["/dashboard/retention/plans", "/dashboard/retention/holds"]


def test_off_mode_refuses_both_posts_and_changes_nothing(client, session_factory, monkeypatch):
    _mode(monkeypatch, "apply")
    plan = _plan(client, session_factory)
    hold = _hold(client)
    for response in (_apply(client, plan), _release(client, hold["hold_id"])):
        assert response.status_code == 403 and response.json()["error"]["code"] == "dashboard_auth_off"
    assert _purge(session_factory, plan["purge_id"]).status == "planned" and _runs(session_factory) == 1
    assert _hold_row(session_factory, hold["hold_id"]).status == "active"


# ---------------------------------------------------------------------------
# permission, CSRF, re-authentication, same origin, confirmation string
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["operator", "auditor", "reviewer"])
def test_roles_without_the_permission_are_refused(client, session_factory, signed_in, monkeypatch, role):
    reset_denial_log()
    _mode(monkeypatch, "apply")
    plan = _plan(client, session_factory)
    hold = _hold(client)
    signed_in(role=role)
    applied, released = _apply(client, plan), _release(client, hold["hold_id"])
    assert applied.status_code == 403 and applied.json()["error"]["permission"] == "retention.apply"
    assert released.status_code == 403 and released.json()["error"]["permission"] == "legal_hold.release"
    assert _purge(session_factory, plan["purge_id"]).status == "planned" and _runs(session_factory) == 1
    assert _hold_row(session_factory, hold["hold_id"]).status == "active"


def test_missing_or_wrong_csrf_token_is_refused(client, session_factory, signed_in, monkeypatch):
    signed_in(role="admin")
    _mode(monkeypatch, "apply")
    plan = _plan(client, session_factory)
    hold = _hold(client)
    wrong = _apply(client, plan, _csrf="not-the-token")
    assert wrong.status_code == 403 and wrong.json()["error"]["code"] == "csrf_token_invalid"
    signed_in.auto_csrf["on"] = False
    for response in (_apply(client, plan), _release(client, hold["hold_id"])):
        assert response.status_code == 403 and response.json()["error"]["code"] == "csrf_token_invalid"
    assert _purge(session_factory, plan["purge_id"]).status == "planned" and _runs(session_factory) == 1
    assert _hold_row(session_factory, hold["hold_id"]).status == "active"


def test_stale_reauthentication_redirects_and_nothing_runs(client, session_factory, signed_in, monkeypatch):
    who = signed_in(role="admin")
    _mode(monkeypatch, "apply")
    plan = _plan(client, session_factory)
    hold = _hold(client)
    _age_reauth(session_factory, who.session_id)
    for response in (_apply(client, plan), _release(client, hold["hold_id"])):
        assert response.status_code == 303 and response.headers["location"].startswith("/dashboard/reauth")
    assert _purge(session_factory, plan["purge_id"]).status == "planned" and _runs(session_factory) == 1
    assert _hold_row(session_factory, hold["hold_id"]).status == "active"


def test_cross_origin_post_is_refused(client, session_factory, signed_in, monkeypatch):
    signed_in(role="admin")
    _mode(monkeypatch, "apply")
    plan = _plan(client, session_factory)
    hold = _hold(client)
    for headers in ({}, {"Origin": "http://evil.example"}):
        assert _apply(client, plan, headers=headers).status_code == 403
        assert _release(client, hold["hold_id"], headers=headers).status_code == 403
    assert _purge(session_factory, plan["purge_id"]).status == "planned"
    assert _hold_row(session_factory, hold["hold_id"]).status == "active"


@pytest.mark.parametrize("confirm", ["", "purge_wrong", "PURGE"])
def test_wrong_confirmation_string_deletes_nothing(client, session_factory, signed_in, monkeypatch, confirm):
    signed_in(role="admin")
    _mode(monkeypatch, "apply")
    plan = _plan(client, session_factory)
    location = _location(_apply(client, plan, confirm=confirm))
    assert "retention_confirm_mismatch" in location and f"purge_id={plan['purge_id']}" in location
    assert _purge(session_factory, plan["purge_id"]).status == "planned" and _runs(session_factory) == 1


def test_plan_mode_shows_the_service_error_and_deletes_nothing(client, session_factory, signed_in, monkeypatch):
    signed_in(role="admin")
    _mode(monkeypatch, "plan")
    plan = _plan(client, session_factory)
    response = _apply(client, plan)
    location = _location(response)
    assert "retention_apply_disabled" in location
    page = client.get(response.headers["location"]).text
    assert "retention_apply_disabled" in page and 'role="alert"' in page
    assert _purge(session_factory, plan["purge_id"]).status == "planned" and _runs(session_factory) == 1


def test_mismatched_plan_digest_is_refused(client, session_factory, signed_in, monkeypatch):
    signed_in(role="admin")
    _mode(monkeypatch, "apply")
    plan = _plan(client, session_factory)
    location = _location(_apply(client, plan, digest="f" * 64))
    assert "retention_plan_digest_mismatch" in location
    assert _purge(session_factory, plan["purge_id"]).status == "planned" and _runs(session_factory) == 1


# ---------------------------------------------------------------------------
# successful apply and release (single person)
# ---------------------------------------------------------------------------


def test_apply_succeeds_and_the_page_shows_the_receipt(client, session_factory, signed_in, monkeypatch):
    who = signed_in(role="admin")
    _mode(monkeypatch, "apply")
    plan = _plan(client, session_factory)
    response = _apply(client, plan)
    location = _location(response)
    purge = _purge(session_factory, plan["purge_id"])
    assert purge.status == "applied" and purge.applied_by == f"user:{who.username}" and purge.receipt_digest
    assert _runs(session_factory) == 0
    assert "1 runs purged, 0 skipped, 0 failures" in location and purge.receipt_digest in location
    html = client.get(response.headers["location"]).text
    assert 'id="apply-receipt"' in html and purge.receipt_digest in html and f"user:{who.username}" in html
    assert re.search(r'id="receipt-runs">1<', html) and re.search(r'id="receipt-failures">0<', html)
    assert 'id="apply-form"' not in html  # an applied plan cannot be applied again from the page
    events = _db(session_factory, lambda s: _all(s, select(GovernanceEvent).where(GovernanceEvent.event_type == "retention.purge_applied")))
    assert [event.user_id for event in events] == [f"user:{who.username}"]


def test_applying_twice_reports_the_service_error(client, session_factory, signed_in, monkeypatch):
    signed_in(role="admin")
    _mode(monkeypatch, "apply")
    plan = _plan(client, session_factory)
    _location(_apply(client, plan))
    again = _location(_apply(client, plan))
    assert "retention_plan_already_applied" in again


def test_expired_report_shows_neither_form_nor_receipt(client, session_factory, signed_in, monkeypatch):
    signed_in(role="admin")
    _mode(monkeypatch, "apply")
    plan = _plan(client, session_factory)

    async def expire(session):
        await session.execute(update(RetentionPurge).where(RetentionPurge.purge_id == plan["purge_id"]).values(status="expired"))
        await session.commit()

    _db(session_factory, expire)
    html = client.get(f"{PAGE}?purge_id={plan['purge_id']}").text
    assert 'id="plan-not-applicable"' in html and 'id="apply-form"' not in html and 'id="apply-receipt"' not in html
    assert "retention_plan_expired" in _location(_apply(client, plan))
    assert _runs(session_factory) == 1


def test_release_succeeds_and_moves_the_hold_to_released(client, session_factory, signed_in, monkeypatch):
    who = signed_in(role="admin")
    _mode(monkeypatch, "plan")
    hold = _hold(client)
    response = _release(client, hold["hold_id"], reason="matter closed")
    location = _location(response)
    assert "holds=released" in location and f"Legal hold {hold['hold_id']} released" in location
    stored = _hold_row(session_factory, hold["hold_id"])
    assert stored.status == "released" and stored.released_by == f"user:{who.username}" and stored.release_reason == "matter closed"
    released = client.get(f"{PAGE}?holds=released").text
    assert hold["hold_id"] in released and "matter closed" in released and f"user:{who.username}" in released
    assert hold["hold_id"] not in client.get(f"{PAGE}?holds=active").text


@pytest.mark.parametrize(("reason", "code"), [("", "legal_hold_release_reason_required"), ("   ", "legal_hold_release_reason_required")])
def test_release_needs_a_reason(client, session_factory, signed_in, monkeypatch, reason, code):
    signed_in(role="admin")
    _mode(monkeypatch, "plan")
    hold = _hold(client)
    assert code in _location(_release(client, hold["hold_id"], reason=reason))
    assert _hold_row(session_factory, hold["hold_id"]).status == "active"


def test_release_of_unknown_or_released_hold_shows_the_service_error(client, session_factory, signed_in, monkeypatch):
    signed_in(role="admin")
    _mode(monkeypatch, "plan")
    assert "legal_hold_not_found" in _location(_release(client, "hold_missing"))
    hold = _hold(client)
    _location(_release(client, hold["hold_id"]))
    assert "legal_hold_not_active" in _location(_release(client, hold["hold_id"]))


def test_dashboard_actions_land_in_the_audit_chain(client, session_factory, signed_in, monkeypatch):
    signed_in(role="admin")
    _mode(monkeypatch, "apply")
    plan = _plan(client, session_factory)
    hold = _hold(client)
    _location(_apply(client, plan))
    _location(_release(client, hold["hold_id"]))
    report = _db(session_factory, lambda s: audit_chain.verify(s, get_settings()))
    assert report["status"] == audit_chain.OK


# ---------------------------------------------------------------------------
# dual control: the forms submit change requests (D39 / D44)
# ---------------------------------------------------------------------------


def test_dual_control_apply_submits_a_change_request_and_a_second_admin_executes(client, session_factory, signed_in, monkeypatch):
    requester = signed_in(role="admin", username="p7-requester")
    _mode(monkeypatch, "apply", dual="retention.apply")
    plan = _plan(client, session_factory)
    page = client.get(f"{PAGE}?purge_id={plan['purge_id']}").text
    assert "Submit change request" in page and "Apply purge plan" not in page
    response = _apply(client, plan, reason="quarterly purge")
    location = _location(response)
    [cr] = _crs(session_factory)
    assert cr.action == "retention.apply" and cr.status == "pending" and cr.requested_by == f"user:{requester.username}"
    assert cr.payload == {"purge_id": plan["purge_id"], "plan_digest": plan["plan_digest"], "confirm": True}
    assert cr.requested_reason == "quarterly purge"
    assert f"change_id={cr.change_id}" in location and "已送出變更覆核" in location
    html = client.get(response.headers["location"]).text
    assert 'id="change-request-link"' in html and cr.change_id in html and re.search(r'id="change-request-status">pending<', html)
    assert _purge(session_factory, plan["purge_id"]).status == "planned" and _runs(session_factory) == 1

    # the requester cannot approve their own request
    own = client.post(f"/dashboard/change-requests/{cr.change_id}/approve", follow_redirects=False)
    assert own.status_code == 403 and own.json()["error"]["code"] == "change_request_self_approval_forbidden"

    approver = signed_in(role="admin", username="p7-approver")
    approved = client.post(f"/dashboard/change-requests/{cr.change_id}/approve", follow_redirects=False)
    assert approved.status_code == 303
    purge = _purge(session_factory, plan["purge_id"])
    assert purge.status == "applied" and purge.applied_by == f"user:{approver.username}" and _runs(session_factory) == 0
    assert _crs(session_factory)[0].status == "executed"


def test_dual_control_release_submits_a_change_request_and_a_second_admin_executes(client, session_factory, signed_in, monkeypatch):
    requester = signed_in(role="admin", username="p7-hold-requester")
    _mode(monkeypatch, "plan", dual="legal_hold.release")
    hold = _hold(client)
    assert "Request release" in client.get(f"{PAGE}?holds=active").text
    location = _location(_release(client, hold["hold_id"], reason="matter closed"))
    [cr] = _crs(session_factory)
    assert cr.action == "legal_hold.release" and cr.requested_by == f"user:{requester.username}" and cr.tenant_id == "t-p7"
    assert cr.payload == {"hold_id": hold["hold_id"], "release_reason": "matter closed"}
    assert f"change_id={cr.change_id}" in location
    assert _hold_row(session_factory, hold["hold_id"]).status == "active"
    # submitting again returns the same pending request
    again = _location(_release(client, hold["hold_id"], reason="matter closed"))
    assert f"change_id={cr.change_id}" in again and "already pending" in again and len(_crs(session_factory)) == 1

    approver = signed_in(role="admin", username="p7-hold-approver")
    assert client.post(f"/dashboard/change-requests/{cr.change_id}/approve", follow_redirects=False).status_code == 303
    stored = _hold_row(session_factory, hold["hold_id"])
    assert stored.status == "released" and stored.released_by == f"user:{approver.username}"
    assert hold["hold_id"] in client.get(f"{PAGE}?holds=released").text


def test_dual_control_still_refuses_up_front_when_the_action_could_never_run(client, session_factory, signed_in, monkeypatch):
    signed_in(role="admin")
    _mode(monkeypatch, "plan", dual="retention.apply,legal_hold.release")
    plan = _plan(client, session_factory)
    assert "retention_apply_disabled" in _location(_apply(client, plan))
    hold = _hold(client)
    assert "legal_hold_release_reason_required" in _location(_release(client, hold["hold_id"], reason=" "))
    assert _crs(session_factory) == []
