"""v1.6.1 P6: dual control with Change Requests (plan D37-D43, feature nine).

Each of the four actions (``retention.apply``, ``legal_hold.release``, ``judge.activate``,
``evaluation_policy.protected_paths``) is requested through its own API endpoint (``202``), then approved, rejected,
cancelled or left to expire. The D40 rules (self-approval, API keys, missing permission, other tenant, re-check at
execution), payload binding (D41), the relaxation-only rule (D42), the job refusal, the audit chain and the
"off by default" compatibility are each tested. Requests from the API use ``AUTH_ENABLED=false`` (requester
``dashboard``) unless a test sets API keys; approvers are signed-in Dashboard users (``signed_in`` fixture).
"""

from __future__ import annotations

import asyncio
import json
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, select, update

from app.core.config import Settings, get_settings
from app.jobs import retention as retention_job
from app.models.access import ChangeRequest
from app.models.agent_run import AgentRun
from app.models.audit_chain import AuditChainEntry
from app.models.governance_event import GovernanceEvent
from app.models.judge import JudgeProfileState
from app.models.retention import LegalHold, RetentionPurge
from app.services import audit_chain, change_requests, operations
from app.services.access.principal import LOCAL_ADMIN
from app.ui.security import reset_denial_log
from tests.test_v160_retention import _add_run

ROOT = Path(__file__).resolve().parents[1]
PAGE = "/dashboard/change-requests"
ALL_ACTIONS = "retention.apply,legal_hold.release,judge.activate,evaluation_policy.protected_paths"


def run(coro):
    return asyncio.run(coro)


def _db(session_factory, fn):
    async def inner():
        async with session_factory() as session:
            return await fn(session)

    return run(inner())


async def _all(session, stmt):
    return list((await session.scalars(stmt)).all())


def _enable(monkeypatch, actions: str = ALL_ACTIONS, **extra) -> None:
    monkeypatch.setenv("DUAL_CONTROL_ACTIONS", actions)
    monkeypatch.setenv("AUDIT_RETENTION_PURGE_MODE", "apply")
    monkeypatch.setenv("EVALUATION_JUDGE_PROFILES", "{}")
    monkeypatch.setenv("EVALUATION_JUDGE_PROFILES_FILE", "")
    for key, value in extra.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()


def _cr(session_factory, change_id: str) -> ChangeRequest:
    return _db(session_factory, lambda s: s.scalar(select(ChangeRequest).where(ChangeRequest.change_id == change_id)))


def _count_crs(session_factory) -> int:
    return _db(session_factory, lambda s: s.scalar(select(func.count()).select_from(ChangeRequest)))


def _events(session_factory, prefix: str = "change_request.") -> list[GovernanceEvent]:
    return _db(session_factory, lambda s: _all(s, select(GovernanceEvent).where(GovernanceEvent.event_type.like(prefix + "%")).order_by(GovernanceEvent.id)))


def _approve(client, change_id: str, **data):
    return client.post(f"{PAGE}/{change_id}/approve", data=data, follow_redirects=False)


def _reject(client, change_id: str, **data):
    return client.post(f"{PAGE}/{change_id}/reject", data=data, follow_redirects=False)


def _code(response) -> str:
    return response.json()["error"]["code"]


# --- seeding helpers ---------------------------------------------------------------------------------------------


def _seed_old_run(session_factory, run_id: str = "run_old") -> None:
    async def seed(session):
        _add_run(session, run_id)
        await session.commit()

    _db(session_factory, seed)


def _runs(session_factory) -> int:
    return _db(session_factory, lambda s: s.scalar(select(func.count()).select_from(AgentRun)))


def _request_apply(client, session_factory) -> tuple[dict, dict]:
    _seed_old_run(session_factory)
    plan = client.post("/v1/retention/plans").json()
    assert plan["status"] == "planned" and len(plan["candidate_run_ids"]) == 1
    response = client.post(f"/v1/retention/plans/{plan['purge_id']}/apply", json={"plan_digest": plan["plan_digest"], "confirm": True})
    assert response.status_code == 202, response.text
    return plan, response.json()


def _hold(client, tenant: str = "t1") -> dict:
    response = client.post("/v1/legal-holds", json={"scope_type": "tenant", "scope_id": tenant, "reason": "litigation QA"})
    assert response.status_code == 200
    return response.json()


def _policy(client, tenant: str, paths: list[str], allow_defaults: bool = True):
    return client.put(f"/v1/tenants/{tenant}/evaluation-policy", json={"protected_paths": paths, "allow_default_protected_paths": allow_defaults})


def _active_judges(session_factory) -> list[str]:
    rows = _db(session_factory, lambda s: _all(s, select(JudgeProfileState).where(JudgeProfileState.active.is_(True))))
    return [row.profile_name for row in rows]


# ---------------------------------------------------------------------------
# off by default: nothing changes (D38)
# ---------------------------------------------------------------------------


def test_default_is_off_and_every_endpoint_behaves_as_before(client, session_factory, monkeypatch):
    monkeypatch.setenv("AUDIT_RETENTION_PURGE_MODE", "apply")
    monkeypatch.setenv("EVALUATION_JUDGE_PROFILES_FILE", "")
    get_settings.cache_clear()
    assert change_requests.configured_actions(get_settings()) == (frozenset(), [])
    _seed_old_run(session_factory)
    plan = client.post("/v1/retention/plans").json()
    applied = client.post(f"/v1/retention/plans/{plan['purge_id']}/apply", json={"plan_digest": plan["plan_digest"], "confirm": True})
    assert applied.status_code == 200 and applied.json()["status"] == "applied"
    hold = _hold(client)
    released = client.post(f"/v1/legal-holds/{hold['hold_id']}/release", json={"release_reason": "closed"})
    assert released.status_code == 200 and released.json()["status"] == "released"
    assert _policy(client, "t1", ["src/a"]).status_code == 200
    relaxed = _policy(client, "t1", [], allow_defaults=False)
    assert relaxed.status_code == 200 and relaxed.json()["effective_protected_paths"] == []
    activated = client.post("/v1/evaluations/judge/profiles/deterministic/activate")
    assert activated.status_code == 200 and activated.json()["active"] is True
    assert _count_crs(session_factory) == 0 and _events(session_factory) == []


def test_settings_parse_known_and_unknown_actions():
    settings = Settings(_env_file=None, dual_control_actions=" Retention.Apply , legal_hold.release,bogus.action ,")
    known, unknown = change_requests.configured_actions(settings)
    assert known == {"retention.apply", "legal_hold.release"} and unknown == ["bogus.action"]
    assert change_requests.is_enabled(settings, "retention.apply") and not change_requests.is_enabled(settings, "judge.activate")
    assert Settings(_env_file=None).dual_control_request_ttl_hours == 24


# ---------------------------------------------------------------------------
# retention.apply
# ---------------------------------------------------------------------------


def test_retention_apply_request_then_second_person_executes(client, session_factory, signed_in, monkeypatch):
    _enable(monkeypatch)
    plan, body = _request_apply(client, session_factory)
    assert body["status"] == "pending_second_approval" and body["created"] is True
    cr = body["change_request"]
    assert cr["change_id"].startswith("chg_") and cr["action"] == "retention.apply" and cr["status"] == "pending"
    assert cr["target_ref"] == f"purge:{plan['purge_id']}" and cr["tenant_id"] is None and cr["requested_by"] == "dashboard"
    assert cr["payload"] == {"purge_id": plan["purge_id"], "plan_digest": plan["plan_digest"], "confirm": True}
    # The plan's TTL caps the request (D40).
    assert datetime.fromisoformat(cr["expires_at"]) <= datetime.fromisoformat(plan["expires_at"]).replace(tzinfo=timezone.utc) + timedelta(seconds=1)
    assert client.get(f"/v1/retention/plans/{plan['purge_id']}").json()["status"] == "planned" and _runs(session_factory) == 1

    approver = signed_in(role="admin")
    response = _approve(client, cr["change_id"], reason="checked the candidates")
    assert response.status_code == 303 and "approved%20and%20executed" in response.headers["location"]
    stored = _cr(session_factory, cr["change_id"])
    assert stored.status == "executed" and stored.decided_by == f"user:{approver.username}" and stored.requested_by == "dashboard"
    assert stored.decided_at is not None and stored.executed_at is not None and stored.decision_reason == "checked the candidates"
    assert stored.result["execution"]["status"] == "applied" and stored.result["execution"]["applied_counts"]["runs"] == 1
    assert _runs(session_factory) == 0
    purge = _db(session_factory, lambda s: s.scalar(select(RetentionPurge).where(RetentionPurge.purge_id == plan["purge_id"])))
    assert purge.status == "applied" and purge.applied_by == f"user:{approver.username}"
    assert [event.event_type for event in _events(session_factory)] == [
        "change_request.created", "change_request.approved", "change_request.executed"
    ]


def test_retention_apply_digest_changed_ends_failed_and_nothing_is_deleted(client, session_factory, signed_in, monkeypatch):
    _enable(monkeypatch)
    plan, body = _request_apply(client, session_factory)

    async def tamper(session):
        await session.execute(update(RetentionPurge).where(RetentionPurge.purge_id == plan["purge_id"]).values(plan_digest="f" * 64))
        await session.commit()

    _db(session_factory, tamper)
    signed_in(role="admin")
    response = _approve(client, body["change_request"]["change_id"])
    assert response.status_code == 303 and "execution%20failed" in response.headers["location"]
    stored = _cr(session_factory, body["change_request"]["change_id"])
    assert stored.status == "failed" and stored.error_code == "retention_plan_digest_mismatch" and stored.executed_at is None
    assert _runs(session_factory) == 1
    assert [event.event_type for event in _events(session_factory)][-1] == "change_request.failed"


def test_retention_apply_mode_switched_back_to_plan_fails_at_approval(client, session_factory, signed_in, monkeypatch):
    _enable(monkeypatch)
    _, body = _request_apply(client, session_factory)
    monkeypatch.setenv("AUDIT_RETENTION_PURGE_MODE", "plan")
    get_settings.cache_clear()
    signed_in(role="admin")
    _approve(client, body["change_request"]["change_id"])
    stored = _cr(session_factory, body["change_request"]["change_id"])
    assert stored.status == "failed" and stored.error_code == "retention_apply_disabled" and _runs(session_factory) == 1


def test_retention_plan_ttl_expires_the_request_and_approval_is_refused(client, session_factory, signed_in, monkeypatch):
    _enable(monkeypatch)
    plan, body = _request_apply(client, session_factory)
    past = datetime.now(timezone.utc) - timedelta(minutes=1)

    async def age(session):
        await session.execute(update(RetentionPurge).where(RetentionPurge.purge_id == plan["purge_id"]).values(expires_at=past))
        await session.execute(update(ChangeRequest).where(ChangeRequest.change_id == body["change_request"]["change_id"]).values(expires_at=past))
        await session.commit()

    _db(session_factory, age)
    signed_in(role="admin")
    response = _approve(client, body["change_request"]["change_id"])
    assert response.status_code == 303 and "change_request_expired" in response.headers["location"]
    assert _cr(session_factory, body["change_request"]["change_id"]).status == "expired" and _runs(session_factory) == 1


def test_retention_apply_request_is_refused_up_front_when_it_could_never_run(client, session_factory, monkeypatch):
    _enable(monkeypatch)
    _seed_old_run(session_factory)
    plan = client.post("/v1/retention/plans").json()
    mismatch = client.post(f"/v1/retention/plans/{plan['purge_id']}/apply", json={"plan_digest": "0" * 64, "confirm": True})
    assert mismatch.status_code == 409 and _code(mismatch) == "retention_plan_digest_mismatch"
    unconfirmed = client.post(f"/v1/retention/plans/{plan['purge_id']}/apply", json={"plan_digest": plan["plan_digest"], "confirm": False})
    assert unconfirmed.status_code == 422 and _code(unconfirmed) == "retention_confirm_required"
    assert _count_crs(session_factory) == 0


def test_duplicate_request_returns_the_same_pending_change(client, session_factory, monkeypatch):
    _enable(monkeypatch)
    plan, body = _request_apply(client, session_factory)
    again = client.post(f"/v1/retention/plans/{plan['purge_id']}/apply", json={"plan_digest": plan["plan_digest"], "confirm": True})
    assert again.status_code == 202 and again.json()["created"] is False
    assert again.json()["change_request"]["change_id"] == body["change_request"]["change_id"] and _count_crs(session_factory) == 1
    assert len(_events(session_factory, "change_request.created")) == 1


def test_retention_job_apply_latest_is_refused_under_dual_control(session_factory, capsys):
    settings = Settings(_env_file=None, audit_retention_purge_mode="apply", dual_control_actions="retention.apply")
    _seed_old_run(session_factory)
    assert retention_job.main(["--plan"], settings=settings, session_factory=session_factory) == 0
    capsys.readouterr()
    assert retention_job.main(["--apply-latest"], settings=settings, session_factory=session_factory) == 2
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "apply_latest" and out["error"]["code"] == "second_approval_required"
    assert _runs(session_factory) == 1 and _count_crs(session_factory) == 0
    # Other actions under dual control do not affect the job.
    other = Settings(_env_file=None, audit_retention_purge_mode="apply", dual_control_actions="legal_hold.release")
    assert retention_job.main(["--apply-latest"], settings=other, session_factory=session_factory) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "applied" and _runs(session_factory) == 0


# ---------------------------------------------------------------------------
# legal_hold.release
# ---------------------------------------------------------------------------


def test_legal_hold_release_request_approve_executes(client, session_factory, signed_in, monkeypatch):
    _enable(monkeypatch)
    hold = _hold(client)
    response = client.post(f"/v1/legal-holds/{hold['hold_id']}/release", json={"release_reason": "matter closed"})
    assert response.status_code == 202
    cr = response.json()["change_request"]
    assert cr["action"] == "legal_hold.release" and cr["tenant_id"] == "t1" and cr["requested_reason"] == "matter closed"
    assert cr["payload"] == {"hold_id": hold["hold_id"], "release_reason": "matter closed"}
    assert client.get("/v1/legal-holds?status=active").json()[0]["hold_id"] == hold["hold_id"]
    approver = signed_in(role="admin")
    _approve(client, cr["change_id"])
    stored = _db(session_factory, lambda s: s.scalar(select(LegalHold).where(LegalHold.hold_id == hold["hold_id"])))
    assert stored.status == "released" and stored.released_by == f"user:{approver.username}" and stored.release_reason == "matter closed"
    assert _cr(session_factory, cr["change_id"]).status == "executed"


def test_legal_hold_release_already_released_ends_failed(client, session_factory, signed_in, monkeypatch):
    _enable(monkeypatch)
    hold = _hold(client)
    cr = client.post(f"/v1/legal-holds/{hold['hold_id']}/release", json={"release_reason": "closed"}).json()["change_request"]

    async def release_elsewhere(session):
        await session.execute(update(LegalHold).where(LegalHold.hold_id == hold["hold_id"]).values(status="released"))
        await session.commit()

    _db(session_factory, release_elsewhere)
    signed_in(role="admin")
    _approve(client, cr["change_id"])
    stored = _cr(session_factory, cr["change_id"])
    assert stored.status == "failed" and stored.error_code == "legal_hold_not_active"


def test_same_target_with_a_different_payload_conflicts(client, session_factory, monkeypatch):
    _enable(monkeypatch)
    hold = _hold(client)
    first = client.post(f"/v1/legal-holds/{hold['hold_id']}/release", json={"release_reason": "closed"})
    second = client.post(f"/v1/legal-holds/{hold['hold_id']}/release", json={"release_reason": "another reason"})
    assert first.status_code == 202 and second.status_code == 409 and _code(second) == "change_request_pending_exists"
    assert second.json()["error"]["details"]["change_id"] == first.json()["change_request"]["change_id"]
    assert _count_crs(session_factory) == 1


def test_reject_executes_nothing_and_is_recorded(client, session_factory, signed_in, monkeypatch):
    _enable(monkeypatch)
    hold = _hold(client)
    cr = client.post(f"/v1/legal-holds/{hold['hold_id']}/release", json={"release_reason": "closed"}).json()["change_request"]
    approver = signed_in(role="admin")
    response = _reject(client, cr["change_id"], reason="hold still needed")
    assert response.status_code == 303 and "rejected" in response.headers["location"]
    stored = _cr(session_factory, cr["change_id"])
    assert stored.status == "rejected" and stored.decided_by == f"user:{approver.username}" and stored.decision_reason == "hold still needed"
    assert client.get("/v1/legal-holds?status=active").json()[0]["hold_id"] == hold["hold_id"]
    assert _approve(client, cr["change_id"]).headers["location"].count("change_request_not_pending") == 1


def test_requester_cancels_through_the_api_and_others_cannot(client, session_factory, signed_in, monkeypatch):
    _enable(monkeypatch)
    hold = _hold(client)
    cr = client.post(f"/v1/legal-holds/{hold['hold_id']}/release", json={"release_reason": "closed"}).json()["change_request"]
    signed_in(role="admin")
    other = client.post(f"{PAGE}/{cr['change_id']}/cancel", follow_redirects=False)
    assert other.status_code == 403 and _code(other) == "change_request_cancel_forbidden"
    cancelled = client.post(f"/v1/change-requests/{cr['change_id']}/cancel", json={"reason": "filed by mistake"})
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled" and cancelled.json()["decided_by"] == "dashboard"
    assert client.get("/v1/legal-holds?status=active").json()[0]["hold_id"] == hold["hold_id"]
    again = client.post(f"/v1/change-requests/{cr['change_id']}/cancel")
    assert again.status_code == 409 and _code(again) == "change_request_not_pending"


def test_dashboard_requester_cancels_own_request(client, session_factory, signed_in, monkeypatch):
    _enable(monkeypatch)
    _policy(client, "t1", ["src/a", "src/b"])
    requester = signed_in(role="tenant_admin", tenant_id="t1")
    client.post("/dashboard/governance/evaluation-policy", data={"tenant_id": "t1", "protected_paths": "src/a", "allow_default_protected_paths": "on"})
    cr = _db(session_factory, lambda s: s.scalar(select(ChangeRequest)))
    assert cr.requested_by == f"user:{requester.username}" and cr.result["requester"]["user_id"] == requester.user_id
    response = client.post(f"{PAGE}/{cr.change_id}/cancel", follow_redirects=False)
    assert response.status_code == 303 and _cr(session_factory, cr.change_id).status == "cancelled"


def test_pending_requests_expire_and_record_an_event(client, session_factory, monkeypatch):
    _enable(monkeypatch)
    hold = _hold(client)
    cr = client.post(f"/v1/legal-holds/{hold['hold_id']}/release", json={"release_reason": "closed"}).json()["change_request"]
    requested = datetime.fromisoformat(cr["requested_at"])
    assert timedelta(hours=23, minutes=59) < datetime.fromisoformat(cr["expires_at"]) - requested <= timedelta(hours=24, seconds=1)

    async def age(session):
        await session.execute(update(ChangeRequest).values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
        await session.commit()

    _db(session_factory, age)
    listed = client.get("/v1/change-requests").json()
    assert listed["count"] == 1 and listed["change_requests"][0]["status"] == "expired"
    assert [event.event_type for event in _events(session_factory)] == ["change_request.created", "change_request.expired"]
    assert client.get("/v1/legal-holds?status=active").json()[0]["hold_id"] == hold["hold_id"]


def test_custom_ttl_setting(client, session_factory, monkeypatch):
    _enable(monkeypatch, DUAL_CONTROL_REQUEST_TTL_HOURS="2")
    hold = _hold(client)
    cr = client.post(f"/v1/legal-holds/{hold['hold_id']}/release", json={"release_reason": "closed"}).json()["change_request"]
    delta = datetime.fromisoformat(cr["expires_at"]) - datetime.fromisoformat(cr["requested_at"])
    assert timedelta(hours=1, minutes=59) < delta <= timedelta(hours=2, seconds=1)


# ---------------------------------------------------------------------------
# judge.activate
# ---------------------------------------------------------------------------


def test_judge_activation_request_and_approval(client, session_factory, signed_in, monkeypatch):
    _enable(monkeypatch)
    response = client.post("/v1/evaluations/judge/profiles/deterministic/activate")
    assert response.status_code == 202 and response.json()["signal_usage"] == "monitor_only"
    cr = response.json()["change_request"]
    assert cr["action"] == "judge.activate" and cr["target_ref"] == "judge:deterministic"
    assert set(cr["payload"]) == {"profile", "config_fingerprint", "probe_fingerprint"}
    assert _active_judges(session_factory) == []
    signed_in(role="admin")
    _approve(client, cr["change_id"])
    assert _cr(session_factory, cr["change_id"]).status == "executed" and _active_judges(session_factory) == ["deterministic"]


def test_judge_probe_changed_after_request_is_stale(client, session_factory, signed_in, monkeypatch):
    _enable(monkeypatch)
    cr = client.post("/v1/evaluations/judge/profiles/deterministic/activate").json()["change_request"]

    async def reprobe(session):
        session.add(JudgeProfileState(profile_name="deterministic", probe_status="ok", probe_fingerprint="0" * 64))
        await session.commit()

    _db(session_factory, reprobe)
    signed_in(role="admin")
    _approve(client, cr["change_id"])
    stored = _cr(session_factory, cr["change_id"])
    assert stored.status == "failed" and stored.error_code == "change_request_stale" and _active_judges(session_factory) == []


def test_unready_judge_profile_is_refused_before_a_request_is_stored(client, session_factory, monkeypatch):
    _enable(monkeypatch)
    refused = client.post("/v1/evaluations/judge/profiles/no-such-profile/activate")
    assert refused.status_code in {404, 409} and _count_crs(session_factory) == 0


def test_dashboard_judge_activation_becomes_a_change_request(client, session_factory, signed_in, monkeypatch):
    _enable(monkeypatch)
    signed_in(role="admin")
    response = client.post(
        "/dashboard/judge-profiles/deterministic/activate", headers={"origin": "http://testserver"}, follow_redirects=False
    )
    assert response.status_code == 303 and "chg_" in response.headers["location"]
    assert _active_judges(session_factory) == [] and _count_crs(session_factory) == 1


# ---------------------------------------------------------------------------
# evaluation_policy.protected_paths (D42: relaxation only)
# ---------------------------------------------------------------------------


def test_adding_protected_paths_needs_no_second_person(client, session_factory, monkeypatch):
    _enable(monkeypatch)
    added = _policy(client, "t1", ["src/a", "src/b"])
    assert added.status_code == 200 and "src/b" in added.json()["effective_protected_paths"]
    more = _policy(client, "t1", ["src/a", "src/b", "src/c"])
    assert more.status_code == 200 and _count_crs(session_factory) == 0


def test_removing_protected_paths_needs_approval_by_tenant_admin(client, session_factory, signed_in, monkeypatch):
    _enable(monkeypatch)
    _policy(client, "t1", ["src/a", "src/b"])
    response = _policy(client, "t1", ["src/a"])
    assert response.status_code == 202
    cr = response.json()["change_request"]
    assert cr["action"] == "evaluation_policy.protected_paths" and cr["tenant_id"] == "t1" and cr["required_permission"] == "evaluation_policy.relax"
    assert "src/b" in client.get("/v1/tenants/t1/evaluation-policy").json()["protected_paths"]
    approver = signed_in(role="tenant_admin", tenant_id="t1")
    _approve(client, cr["change_id"])
    policy = client.get("/v1/tenants/t1/evaluation-policy").json()
    assert policy["protected_paths"] == ["src/a"] and policy["updated_by"] == f"user:{approver.username}"
    assert _cr(session_factory, cr["change_id"]).status == "executed"


def test_turning_default_paths_off_is_a_relaxation(client, session_factory, monkeypatch):
    _enable(monkeypatch)
    response = _policy(client, "t1", [], allow_defaults=False)
    assert response.status_code == 202 and client.get("/v1/tenants/t1/evaluation-policy").json()["allow_default_protected_paths"] is True


def test_policy_changed_after_request_is_stale(client, session_factory, signed_in, monkeypatch):
    _enable(monkeypatch)
    _policy(client, "t1", ["src/a", "src/b"])
    cr = _policy(client, "t1", ["src/a"]).json()["change_request"]
    assert _policy(client, "t1", ["src/a", "src/b", "src/c"]).status_code == 200  # tightening applies at once
    signed_in(role="admin")
    _approve(client, cr["change_id"])
    stored = _cr(session_factory, cr["change_id"])
    assert stored.status == "failed" and stored.error_code == "change_request_stale"
    assert client.get("/v1/tenants/t1/evaluation-policy").json()["protected_paths"] == ["src/a", "src/b", "src/c"]


def test_dashboard_relaxation_becomes_a_change_request_and_tightening_applies(client, session_factory, signed_in, monkeypatch):
    _enable(monkeypatch)
    signed_in(role="admin")
    tighten = client.post("/dashboard/governance/evaluation-policy", data={"tenant_id": "t1", "protected_paths": "src/a\nsrc/b", "allow_default_protected_paths": "on"}, follow_redirects=False)
    assert tighten.status_code == 303 and _count_crs(session_factory) == 0
    relax = client.post("/dashboard/governance/evaluation-policy", data={"tenant_id": "t1", "protected_paths": "src/a", "allow_default_protected_paths": "on"}, follow_redirects=False)
    assert relax.status_code == 303 and "chg_" in relax.headers["location"]
    assert client.get("/v1/tenants/t1/evaluation-policy").json()["protected_paths"] == ["src/a", "src/b"]


# ---------------------------------------------------------------------------
# D40: who may decide
# ---------------------------------------------------------------------------


def test_requester_cannot_approve_or_reject_their_own_request(client, session_factory, signed_in, monkeypatch):
    _enable(monkeypatch)
    _policy(client, "t1", ["src/a", "src/b"])
    signed_in(role="admin")
    client.post("/dashboard/governance/evaluation-policy", data={"tenant_id": "t1", "protected_paths": "src/a", "allow_default_protected_paths": "on"})
    cr = _db(session_factory, lambda s: s.scalar(select(ChangeRequest)))
    for action in (_approve, _reject):
        response = action(client, cr.change_id)
        assert response.status_code == 403 and _code(response) == "change_request_self_approval_forbidden"
    assert _cr(session_factory, cr.change_id).status == "pending"
    page = client.get(PAGE)
    assert 'data-testid="change-request-approve"' not in page.text and "another person must approve it" in page.text


def test_api_keys_cannot_approve(client, session_factory, monkeypatch):
    _enable(monkeypatch)
    hold = _hold(client)
    cr = client.post(f"/v1/legal-holds/{hold['hold_id']}/release", json={"release_reason": "closed"}).json()["change_request"]
    for path in ("approve", "reject", "decision"):
        assert client.post(f"/v1/change-requests/{cr['change_id']}/{path}").status_code in {404, 405}

    async def try_service(session):
        settings = get_settings()
        for principal in (None, LOCAL_ADMIN):
            with pytest.raises(change_requests.ChangeRequestError) as exc:
                await change_requests.approve(session, settings, cr["change_id"], principal)
            assert exc.value.code == "change_request_sign_in_required" and exc.value.status_code == 403

    _db(session_factory, try_service)
    # Dashboard sign-in off (synthetic local admin): approval is refused too (D10).
    response = _approve(client, cr["change_id"])
    assert response.status_code == 403 and _code(response) == "change_request_sign_in_required"
    assert _cr(session_factory, cr["change_id"]).status == "pending"


def test_approver_needs_the_permission_of_the_action(client, session_factory, signed_in, monkeypatch):
    _enable(monkeypatch)
    _, body = _request_apply(client, session_factory)
    change_id = body["change_request"]["change_id"]
    reset_denial_log()
    signed_in(role="operator")
    assert _approve(client, change_id).status_code == 403  # no change_requests.decide at all
    signed_in(role="tenant_admin", tenant_id="t1")  # decide, but not retention.apply (and a platform request)
    response = _approve(client, change_id)
    assert response.status_code == 403 and _code(response) == "tenant_scope_denied"
    assert _cr(session_factory, change_id).status == "pending" and _runs(session_factory) == 1
    # Same tenant, but releasing a legal hold needs legal_hold.release (admin): the service refuses it.
    hold = _hold(client, "t1")
    hold_cr = client.post(f"/v1/legal-holds/{hold['hold_id']}/release", json={"release_reason": "closed"}).json()["change_request"]
    refused = _approve(client, hold_cr["change_id"])
    assert refused.status_code == 403 and _code(refused) == "change_request_forbidden"
    assert refused.json()["error"]["details"]["permission"] == "legal_hold.release"
    assert _cr(session_factory, hold_cr["change_id"]).status == "pending"


def test_tenant_bound_approver_cannot_decide_another_tenant(client, session_factory, signed_in, monkeypatch):
    _enable(monkeypatch)
    _policy(client, "t1", ["src/a", "src/b"])
    cr = _policy(client, "t1", ["src/a"]).json()["change_request"]
    reset_denial_log()
    signed_in(role="tenant_admin", tenant_id="t2")
    response = _approve(client, cr["change_id"])
    assert response.status_code == 403 and _code(response) == "tenant_scope_denied"
    assert cr["change_id"] not in client.get(PAGE).text  # the list is tenant-scoped

    async def service_rule(session):
        cr_row = await change_requests.get_change_request(session, cr["change_id"])
        principal = LOCAL_ADMIN.__class__(**{**LOCAL_ADMIN.__dict__, "user_id": "usr_x", "username": "x", "role": "tenant_admin",
                                             "tenant_id": "t2", "session_id": "ses_x", "synthetic": False,
                                             "reauth_at": datetime.now(timezone.utc)})
        with pytest.raises(change_requests.ChangeRequestError) as exc:
            change_requests._check_decider(cr_row, principal, get_settings())
        assert exc.value.code == "change_request_tenant_forbidden"

    _db(session_factory, service_rule)
    assert _cr(session_factory, cr["change_id"]).status == "pending"


def test_stale_reauthentication_is_sent_to_reauth_before_anything_runs(client, session_factory, signed_in, monkeypatch):
    _enable(monkeypatch)
    hold = _hold(client)
    cr = client.post(f"/v1/legal-holds/{hold['hold_id']}/release", json={"release_reason": "closed"}).json()["change_request"]
    who = signed_in(role="admin")
    from app.models.access import DashboardSession

    async def age(session):
        await session.execute(
            update(DashboardSession).where(DashboardSession.session_id == who.session_id).values(reauth_at=datetime.now(timezone.utc) - timedelta(hours=1))
        )
        await session.commit()

    _db(session_factory, age)
    response = _approve(client, cr["change_id"])
    assert response.status_code == 303 and response.headers["location"].startswith("/dashboard/reauth")
    assert _cr(session_factory, cr["change_id"]).status == "pending"


# ---------------------------------------------------------------------------
# API read, events and audit chain, Dashboard page, contracts, operations
# ---------------------------------------------------------------------------


def test_api_list_get_and_tenant_scope(client, session_factory, monkeypatch):
    _enable(monkeypatch)
    hold = _hold(client, "t1")
    cr = client.post(f"/v1/legal-holds/{hold['hold_id']}/release", json={"release_reason": "closed"}).json()["change_request"]
    assert client.get(f"/v1/change-requests/{cr['change_id']}").json()["status"] == "pending"
    assert client.get("/v1/change-requests?status=pending&action=legal_hold.release").json()["count"] == 1
    assert client.get("/v1/change-requests?status=executed").json()["count"] == 0
    missing = client.get("/v1/change-requests/chg_missing")
    assert missing.status_code == 404 and _code(missing) == "change_request_not_found"
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_KEYS", "k-plat:default:admin,k-t1:t1:viewer,k-t2:t2:viewer,k-op:t1:operator")
    get_settings.cache_clear()
    assert client.get("/v1/change-requests", headers={"X-API-Key": "k-plat"}).json()["count"] == 1
    assert client.get("/v1/change-requests", headers={"X-API-Key": "k-t1"}).json()["count"] == 1
    assert client.get("/v1/change-requests", headers={"X-API-Key": "k-t2"}).json()["count"] == 0
    assert client.get(f"/v1/change-requests/{cr['change_id']}", headers={"X-API-Key": "k-t2"}).status_code == 404
    assert client.get("/v1/change-requests?tenant_id=t1", headers={"X-API-Key": "k-t2"}).status_code == 403
    assert client.post(f"/v1/change-requests/{cr['change_id']}/cancel", headers={"X-API-Key": "k-op"}).status_code == 403
    other_admin = client.post(f"/v1/change-requests/{cr['change_id']}/cancel", headers={"X-API-Key": "k-plat"})
    assert other_admin.status_code == 403 and _code(other_admin) == "change_request_cancel_forbidden"


def test_api_key_requester_is_recorded_and_can_cancel(client, session_factory, monkeypatch):
    _enable(monkeypatch, AUTH_ENABLED="true", API_KEYS="k-plat:default:admin,k-two:default:admin")
    headers = {"X-API-Key": "k-plat"}
    hold = client.post("/v1/legal-holds", json={"scope_type": "tenant", "scope_id": "t1", "reason": "qa"}, headers=headers).json()
    cr = client.post(f"/v1/legal-holds/{hold['hold_id']}/release", json={"release_reason": "closed"}, headers=headers).json()["change_request"]
    assert cr["requested_by"] == "key:legacy"
    assert client.post(f"/v1/change-requests/{cr['change_id']}/cancel", headers=headers).json()["status"] == "cancelled"


def test_change_request_events_are_chained_and_verify(client, session_factory, signed_in, monkeypatch):
    _enable(monkeypatch)
    hold = _hold(client)
    first = client.post(f"/v1/legal-holds/{hold['hold_id']}/release", json={"release_reason": "closed"}).json()["change_request"]
    signed_in(role="admin")
    _reject(client, first["change_id"])
    second = client.post(f"/v1/legal-holds/{hold['hold_id']}/release", json={"release_reason": "closed"}).json()["change_request"]
    assert second["change_id"] != first["change_id"]
    signed_in(role="admin")
    _approve(client, second["change_id"])
    events = _events(session_factory)
    assert [event.event_type for event in events] == [
        "change_request.created", "change_request.rejected", "change_request.created", "change_request.approved", "change_request.executed"
    ]
    assert all(event.details["change_id"] in {first["change_id"], second["change_id"]} for event in events)
    assert all("release_reason" not in json.dumps(event.details) for event in events)  # digests and ids, not payloads
    chained = _db(session_factory, lambda s: _all(s, select(AuditChainEntry.ref_id).where(AuditChainEntry.ref_table == "governance_events")))
    assert {str(event.id) for event in events} <= set(chained)
    report = _db(session_factory, lambda s: audit_chain.verify(s, get_settings()))
    assert report["status"] == audit_chain.OK


def test_dashboard_page_lists_and_shows_buttons_by_role(client, session_factory, signed_in, monkeypatch):
    _enable(monkeypatch)
    hold = _hold(client)
    platform_cr = client.post("/v1/evaluations/judge/profiles/deterministic/activate").json()["change_request"]
    hold_cr = client.post(f"/v1/legal-holds/{hold['hold_id']}/release", json={"release_reason": "closed"}).json()["change_request"]
    signed_in(role="admin")
    page = client.get(PAGE)
    assert page.status_code == 200 and "Change Requests" in page.text and "變更覆核" in page.text
    assert platform_cr["change_id"] in page.text and hold_cr["change_id"] in page.text
    assert page.text.count('data-testid="change-request-approve"') == 2 and 'name="_csrf"' in page.text
    assert 'href="/dashboard/change-requests"' in page.text  # navigation item
    signed_in(role="auditor")
    auditor_page = client.get(PAGE)
    assert auditor_page.status_code == 200 and 'data-testid="change-request-approve"' not in auditor_page.text
    signed_in(role="tenant_admin", tenant_id="t1")
    tenant_page = client.get(PAGE)
    assert hold_cr["change_id"] in tenant_page.text and platform_cr["change_id"] not in tenant_page.text
    assert 'data-testid="change-request-approve"' not in tenant_page.text  # needs legal_hold.release
    assert client.get(PAGE + "?tenant_id=t2").status_code == 403


def test_dashboard_page_in_off_mode_lists_without_decision_buttons(client, session_factory, monkeypatch):
    _enable(monkeypatch)
    hold = _hold(client)
    client.post(f"/v1/legal-holds/{hold['hold_id']}/release", json={"release_reason": "closed"})
    page = client.get(PAGE)
    assert page.status_code == 200 and 'data-testid="change-request-sign-in-off"' in page.text
    assert 'data-testid="change-request-approve"' not in page.text and 'data-testid="change-request-row"' in page.text


def test_import_linter_contract_covers_change_requests():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    contract = next(item for item in config["tool"]["importlinter"]["contracts"] if "change requests" in item["name"])
    assert "app.services.change_requests" in contract["source_modules"]
    assert {"app.services.events", "app.infra.kafka", "app.workers", "app.services.finops.leases"} <= set(contract["forbidden_modules"])


def test_change_requests_never_touch_runs_kafka_or_leases(client, session_factory, signed_in, monkeypatch):
    _enable(monkeypatch)
    _policy(client, "t1", ["src/a", "src/b"])
    cr = _policy(client, "t1", ["src/a"]).json()["change_request"]
    published_before = len(client.event_publisher.events)
    signed_in(role="admin")
    _approve(client, cr["change_id"])
    assert _cr(session_factory, cr["change_id"]).status == "executed"
    assert len(client.event_publisher.events) == published_before
    assert _runs(session_factory) == 0


@pytest.mark.parametrize(
    ("env", "actions", "status", "advisory"),
    [
        ("dev", "", "warn", True),
        ("prod", "", "warn", False),
        ("prod", "retention.apply", "warn", False),
        ("prod", "retention.apply,legal_hold.release", "pass", False),
        ("dev", "retention.apply,legal_hold.release,unknown.thing", "warn", False),
    ],
)
def test_operations_check_dual_control_configured(env, actions, status, advisory):
    check = operations._dual_control_configured_check(Settings(_env_file=None, app_env=env, dual_control_actions=actions))
    assert check.check_key == "dual_control_configured" and check.status == status and check.metadata["advisory"] is advisory


def test_dual_control_check_is_in_the_operations_readiness(client):
    body = client.get("/v1/operations/readiness").json()
    check = next(item for item in body["checks"] if item["check_key"] == "dual_control_configured")
    assert check["status"] == "warn" and check["metadata"]["advisory"] is True
