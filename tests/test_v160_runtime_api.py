"""v1.6.0 P5: retention / legal hold / sandbox / tracing endpoints, 410 ``run_purged`` and operations checks.

The service rules are covered in ``test_v160_retention.py`` and ``test_v160_sandbox.py``; these tests check the
HTTP mapping: status codes, ``{"error": {"code"}}`` bodies, principal-to-role mapping and the router split.
Settings come from environment variables set per test (never the developer's ``.env`` defaults).
"""

from __future__ import annotations

import asyncio

import pytest

from app.api.routers import operations as operations_router
from app.api.routers import retention as retention_router
from app.api.routers import sandbox as sandbox_router
from app.core.config import get_settings
from app.main import app
from tests.test_v160_retention import _add_full_children, _add_run

RUN_SUBRESOURCES = (
    "/v1/runs/{id}",
    "/v1/runs/{id}/trace",
    "/v1/runs/{id}/replay-bundle",
    "/v1/runs/{id}/execution-plan",
    "/v1/runs/{id}/checkpoints",
    "/v1/runs/{id}/worker-attempts",
    "/v1/runs/{id}/tool-executions",
    "/v1/runs/{id}/incidents",
    "/v1/runs/{id}/outcomes",
)
API_KEYS = "adm:default:admin,sup:default:super_admin,ta1:t1:tenant_admin,ta2:t2:tenant_admin,view:t1:viewer"


def _env(monkeypatch, **values):
    base = {
        "AUTH_ENABLED": "false",
        "API_KEYS": "",
        "AUDIT_RETENTION_PURGE_MODE": "plan",
        "AUDIT_RETENTION_DAYS": "90",
        "AUDIT_RETENTION_PURGE_BATCH_SIZE": "500",
        "AUDIT_RETENTION_PLAN_TTL_MINUTES": "60",
        "FINOPS_LEDGER_RETENTION_DAYS": "2555",
        "SANDBOX_BACKEND": "simulated",
        "SANDBOX_MAX_TIMEOUT_SECONDS": "300",
        "SANDBOX_MAX_OUTPUT_BYTES": "1048576",
    }
    base.update(values)
    for key, value in base.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()


def _seed(session_factory, *run_ids, tenant="t1", full=True):
    async def scenario():
        async with session_factory() as session:
            for run_id in run_ids:
                _add_run(session, run_id, tenant=tenant)
                if full:
                    _add_full_children(session, run_id)
            await session.commit()

    asyncio.run(scenario())


def _code(response) -> str:
    return response.json()["error"]["code"]


def _purge(client, monkeypatch, session_factory, run_id="run_api_old", **seed):
    _env(monkeypatch, AUDIT_RETENTION_PURGE_MODE="apply")
    _seed(session_factory, run_id, **seed)
    plan = client.post("/v1/retention/plans").json()
    assert run_id in plan["candidate_run_ids"]
    applied = client.post(
        f"/v1/retention/plans/{plan['purge_id']}/apply", json={"plan_digest": plan["plan_digest"], "confirm": True}
    )
    assert applied.status_code == 200, applied.text
    return plan, applied.json()


def test_v160_policy_endpoint_reports_default_plan_mode(client, monkeypatch):
    _env(monkeypatch)
    body = client.get("/v1/retention/policy").json()
    assert body["purge_mode"] == "plan" and body["deletes_enabled"] is False
    assert body["retention_days"] == 90 and body["finops_ledger_policy"] == "report_only"
    assert "unexported_label" in body["exclusion_reasons"] and "legal_hold" in body["exclusion_reasons"]


def test_v160_plan_then_apply_in_default_mode_is_refused(client, monkeypatch, session_factory):
    _env(monkeypatch)
    _seed(session_factory, "run_plan_only")
    plan = client.post("/v1/retention/plans")
    assert plan.status_code == 200
    body = plan.json()
    assert body["status"] == "planned" and body["candidate_run_ids"] == ["run_plan_only"]
    assert client.get(f"/v1/retention/plans/{body['purge_id']}").json()["plan_digest"] == body["plan_digest"]
    refused = client.post(
        f"/v1/retention/plans/{body['purge_id']}/apply", json={"plan_digest": body["plan_digest"], "confirm": True}
    )
    assert refused.status_code == 409 and _code(refused) == "retention_apply_disabled"
    assert client.get("/v1/runs/run_plan_only").status_code == 200


def test_v160_mode_off_refuses_plan_with_error_shape(client, monkeypatch):
    _env(monkeypatch, AUDIT_RETENTION_PURGE_MODE="off")
    refused = client.post("/v1/retention/plans")
    assert refused.status_code == 409
    assert refused.json() == {"error": {"code": "retention_disabled", "message": "AUDIT_RETENTION_PURGE_MODE is off."}}


@pytest.mark.parametrize(
    ("payload", "status", "code"),
    [
        ({"plan_digest": "0" * 64, "confirm": True}, 409, "retention_plan_digest_mismatch"),
        ({"plan_digest": "DIGEST", "confirm": False}, 422, "retention_confirm_required"),
    ],
)
def test_v160_apply_refusals_map_to_http(client, monkeypatch, session_factory, payload, status, code):
    _env(monkeypatch, AUDIT_RETENTION_PURGE_MODE="apply")
    _seed(session_factory, "run_refused")
    plan = client.post("/v1/retention/plans").json()
    if payload["plan_digest"] == "DIGEST":
        payload = {**payload, "plan_digest": plan["plan_digest"]}
    refused = client.post(f"/v1/retention/plans/{plan['purge_id']}/apply", json=payload)
    assert refused.status_code == status and _code(refused) == code
    assert client.get("/v1/runs/run_refused").status_code == 200


def test_v160_unknown_plan_returns_404_codes(client, monkeypatch):
    _env(monkeypatch, AUDIT_RETENTION_PURGE_MODE="apply")
    assert _code(client.get("/v1/retention/plans/purge_missing")) == "retention_plan_not_found"
    missing = client.post("/v1/retention/plans/purge_missing/apply", json={"plan_digest": "x", "confirm": True})
    assert missing.status_code == 404 and _code(missing) == "retention_plan_not_found"


def test_v160_apply_purges_and_run_subresources_answer_410(client, monkeypatch, session_factory):
    plan, receipt = _purge(client, monkeypatch, session_factory)
    assert receipt["status"] == "applied" and receipt["applied_counts"]["runs"] == 1 and receipt["receipt_digest"]
    for template in RUN_SUBRESOURCES:
        response = client.get(template.format(id="run_api_old"))
        assert response.status_code == 410, (template, response.status_code)
        body = response.json()
        assert body["error"]["code"] == "run_purged"
        assert body["tombstone"]["run_id"] == "run_api_old" and body["tombstone"]["purge_id"] == plan["purge_id"]
        assert "PROMPT-CONTENT" not in response.text
    for path in ("/v1/runs/run_api_old/replay", "/v1/runs/run_api_old/resume"):
        assert client.post(path, json={"created_by": "t", "resume_reason": "retry"}).status_code == 410
    again = client.post(
        f"/v1/retention/plans/{plan['purge_id']}/apply", json={"plan_digest": plan["plan_digest"], "confirm": True}
    )
    assert again.status_code == 409 and _code(again) == "retention_plan_already_applied"
    # An id that never existed keeps the historical 404.
    assert client.get("/v1/runs/run_never_existed").status_code == 404


def test_v160_purges_and_tombstones_are_listed(client, monkeypatch, session_factory):
    plan, _ = _purge(client, monkeypatch, session_factory, "run_listed")
    purges = client.get("/v1/retention/purges").json()
    assert purges[0]["purge_id"] == plan["purge_id"] and purges[0]["status"] == "applied"
    assert client.get("/v1/retention/purges?status=planned").json() == []
    tombstones = client.get(f"/v1/retention/tombstones?purge_id={plan['purge_id']}").json()
    assert [item["run_id"] for item in tombstones] == ["run_listed"]
    assert tombstones[0]["record_counts"]["agent_runs"] == 1 and len(tombstones[0]["content_digest"]) == 64
    assert client.get("/v1/retention/tombstones?tenant_id=other").json() == []


def test_v160_purged_run_tombstone_is_tenant_scoped(client, monkeypatch, session_factory):
    _purge(client, monkeypatch, session_factory, "run_scoped")
    _env(monkeypatch, AUDIT_RETENTION_PURGE_MODE="apply", AUTH_ENABLED="true", API_KEYS=API_KEYS)
    other = client.get("/v1/runs/run_scoped", headers={"X-API-Key": "ta2"})
    assert other.status_code == 403 and "tombstone" not in other.json()
    own = client.get("/v1/runs/run_scoped", headers={"X-API-Key": "view"})
    assert own.status_code == 410 and own.json()["tombstone"]["tenant_id"] == "t1"
    tombstones = client.get("/v1/retention/tombstones", headers={"X-API-Key": "ta2"}).json()
    assert tombstones == []


def test_v160_retention_endpoint_roles(client, monkeypatch):
    _env(monkeypatch, AUTH_ENABLED="true", API_KEYS=API_KEYS)
    assert client.get("/v1/retention/policy", headers={"X-API-Key": "view"}).status_code == 200
    assert client.post("/v1/retention/plans", headers={"X-API-Key": "view"}).status_code == 403
    assert client.post("/v1/retention/plans", headers={"X-API-Key": "ta1"}).status_code == 403
    assert client.get("/v1/retention/purges", headers={"X-API-Key": "view"}).status_code == 403
    assert client.post("/v1/retention/plans", headers={"X-API-Key": "sup"}).status_code == 200
    assert client.get("/v1/retention/purges", headers={"X-API-Key": "adm"}).status_code == 200


def test_v160_legal_hold_roles_and_release(client, monkeypatch, session_factory):
    _env(monkeypatch, AUTH_ENABLED="true", API_KEYS=API_KEYS)
    _seed(session_factory, "run_t2", tenant="t2", full=False)
    own = client.post(
        "/v1/legal-holds", headers={"X-API-Key": "ta1"}, json={"scope_type": "tenant", "scope_id": "t1", "reason": "litigation"}
    )
    assert own.status_code == 200 and own.json()["status"] == "active" and own.json()["hold_id"].startswith("lh_")
    hold_id = own.json()["hold_id"]
    cross = client.post(
        "/v1/legal-holds", headers={"X-API-Key": "ta1"}, json={"scope_type": "run", "scope_id": "run_t2", "reason": "x"}
    )
    assert cross.status_code == 403 and _code(cross) == "legal_hold_forbidden"
    viewer = client.post(
        "/v1/legal-holds", headers={"X-API-Key": "view"}, json={"scope_type": "tenant", "scope_id": "t1", "reason": "x"}
    )
    assert viewer.status_code == 403
    admin_any = client.post(
        "/v1/legal-holds", headers={"X-API-Key": "adm"}, json={"scope_type": "run", "scope_id": "run_t2", "reason": "audit"}
    )
    assert admin_any.status_code == 200 and admin_any.json()["tenant_id"] == "t2"
    bad_scope = client.post(
        "/v1/legal-holds", headers={"X-API-Key": "adm"}, json={"scope_type": "org", "scope_id": "t1", "reason": "x"}
    )
    assert bad_scope.status_code == 422 and _code(bad_scope) == "legal_hold_scope_invalid"
    # Listing is tenant-scoped for tenant keys.
    listed = client.get("/v1/legal-holds", headers={"X-API-Key": "ta1"}).json()
    assert [item["hold_id"] for item in listed] == [hold_id]
    # Release: admin only, with a reason.
    denied = client.post(f"/v1/legal-holds/{hold_id}/release", headers={"X-API-Key": "ta1"}, json={"release_reason": "done"})
    assert denied.status_code == 403 and _code(denied) == "legal_hold_forbidden"
    no_reason = client.post(f"/v1/legal-holds/{hold_id}/release", headers={"X-API-Key": "adm"}, json={})
    assert no_reason.status_code == 422 and _code(no_reason) == "legal_hold_release_reason_required"
    released = client.post(
        f"/v1/legal-holds/{hold_id}/release", headers={"X-API-Key": "sup"}, json={"release_reason": "case closed"}
    )
    assert released.status_code == 200 and released.json()["status"] == "released"
    assert released.json()["release_reason"] == "case closed"
    again = client.post(f"/v1/legal-holds/{hold_id}/release", headers={"X-API-Key": "adm"}, json={"release_reason": "x"})
    assert again.status_code == 409 and _code(again) == "legal_hold_not_active"
    missing = client.post("/v1/legal-holds/lh_missing/release", headers={"X-API-Key": "adm"}, json={"release_reason": "x"})
    assert missing.status_code == 404 and _code(missing) == "legal_hold_not_found"
    # With auth enabled every key is tenant-bound for listings (same rule as /v1/runs): t2 sees its run hold.
    active_t2 = client.get("/v1/legal-holds?status=active", headers={"X-API-Key": "ta2"}).json()
    assert [item["scope_id"] for item in active_t2] == ["run_t2"]


def test_v160_hold_on_unknown_run_is_404(client, monkeypatch):
    _env(monkeypatch)
    missing = client.post("/v1/legal-holds", json={"scope_type": "run", "scope_id": "run_nope", "reason": "x"})
    assert missing.status_code == 404 and _code(missing) == "run_not_found"


def test_v160_ledger_report_endpoint_never_deletes(client, monkeypatch, session_factory):
    _purge(client, monkeypatch, session_factory, "run_ledger")
    _env(monkeypatch, AUDIT_RETENTION_PURGE_MODE="apply", FINOPS_LEDGER_RETENTION_DAYS="30")
    body = client.get("/v1/retention/finops-ledger-report").json()
    assert body["policy"] == "report_only" and body["deletes"] is False and body["retention_days"] == 30
    assert body["tables"]["usage_events"]["expired_count"] == 1
    assert body["tables"]["cost_events"]["expired_count"] == 1
    assert body["tables"]["budget_violations"]["expired_count"] == 1
    # The FinOps ledger of a purged run is still readable (D14).
    assert client.get("/v1/usage-events/runs/run_ledger").status_code == 200


def test_v160_sandbox_status_reports_simulated_backend(client, monkeypatch):
    _env(monkeypatch)
    body = client.get("/v1/sandbox/status").json()
    assert body["status"] == "pass" and body["configured_backend"] == "simulated"
    assert body["capabilities"]["backend_id"] == "simulated" and body["capabilities"]["isolation_level"] == "simulated"
    assert body["executes_tools"] is False and body["smoke"]["passed"] is True
    assert {case["name"] for case in body["smoke"]["cases"]} >= {"rejects_invalid_spec", "limits_required"}
    assert body["isolation_policy"]["mode"] == "record_only"
    assert body["isolation_policy"]["min_isolation_high_risk"] == "gvisor"


def test_v160_sandbox_status_fails_closed_on_bad_config(client, monkeypatch):
    _env(monkeypatch, SANDBOX_MAX_TIMEOUT_SECONDS="0")
    response = client.get("/v1/sandbox/status")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "fail" and body["capabilities"] is None and "greater than 0" in body["error"]


def test_v160_operations_checks_retention_and_sandbox(client, monkeypatch):
    _env(monkeypatch)
    client.post("/v1/legal-holds", json={"scope_type": "tenant", "scope_id": "t9", "reason": "x"})
    client.post("/v1/retention/plans")
    checks = {item["check_key"]: item for item in client.get("/v1/operations/readiness").json()["checks"]}
    assert "audit_retention_config" not in checks
    retention_check = checks["retention_policy"]
    assert retention_check["status"] == "pass"
    assert retention_check["metadata"]["purge_mode"] == "plan" and retention_check["metadata"]["active_holds"] == 1
    assert retention_check["metadata"]["latest_plan_at"] and "never deletes" in retention_check["detail"]
    sandbox_check = checks["sandbox_backend_health"]
    assert sandbox_check["status"] == "pass" and sandbox_check["metadata"]["backend_id"] == "simulated"
    assert sandbox_check["metadata"]["executes_tools"] is False


def test_v160_operations_sandbox_check_fails_on_bad_backend(client, monkeypatch):
    _env(monkeypatch, AUDIT_RETENTION_DAYS="0", SANDBOX_MAX_OUTPUT_BYTES="0")
    checks = {item["check_key"]: item for item in client.get("/v1/operations/readiness").json()["checks"]}
    assert checks["sandbox_backend_health"]["status"] == "fail"
    assert checks["retention_policy"]["status"] == "fail"


def test_v160_tracing_endpoint_exposes_host_only(client, monkeypatch):
    _env(monkeypatch)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://user:pw-zq7@collector.internal:4318/v1")
    response = client.get("/v1/observability/tracing")
    body = response.json()
    assert body["w3c_propagation"] is True and body["exporter_host"] == "collector.internal"
    assert isinstance(body["dropped_attributes"], int) and body["dropped_attributes"] >= 0
    assert "pw-zq7" not in response.text


def test_v160_new_and_moved_paths_are_served_by_domain_routers():
    owners = {
        (route.path, method): route.endpoint.__module__
        for route in app.routes
        if hasattr(route, "methods")
        for method in route.methods
    }
    expected = {
        ("/v1/operations/readiness", "GET"): operations_router.__name__,
        ("/v1/operations/evidence", "POST"): operations_router.__name__,
        ("/v1/operations/release-gates/{gate_id}", "GET"): operations_router.__name__,
        ("/v1/observability/summary", "GET"): operations_router.__name__,
        ("/v1/observability/tracing", "GET"): operations_router.__name__,
        ("/v1/retention/plans/{purge_id}/apply", "POST"): retention_router.__name__,
        ("/v1/legal-holds/{hold_id}/release", "POST"): retention_router.__name__,
        ("/v1/sandbox/status", "GET"): sandbox_router.__name__,
    }
    for key, module in expected.items():
        assert owners.get(key) == module, key
