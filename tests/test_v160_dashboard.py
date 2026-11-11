"""v1.6.0 P6: ``/dashboard/retention``, Run Detail (purged notice, trace link, legal hold, sandbox_execution),
Observability Tracing panel, Operations checks and the operations / observability UI router move.

Dashboard writes here are limited to a dry-run plan and creating a legal hold, both behind the same-origin check;
applying a plan and releasing a hold have no Dashboard route in this release (v1.6.1 RBAC / CSRF).
"""

from __future__ import annotations

import asyncio
import re

from app.core.config import get_settings
from app.main import app
from app.models.agent_run import AgentRun
from app.ui.routers import operations as ui_operations
from app.ui.routers import retention as ui_retention
from tests.test_v160_retention import _add_run

SAME_ORIGIN = {"Origin": "http://testserver"}
TRACE_HEX = "4bf92f3577b34da6a3ce929d0e0e4736"
SPEC_DIGEST = "ab12cd34ef56" + "0" * 52


def _env(monkeypatch, **values):
    base = {
        "AUTH_ENABLED": "false",
        "DASHBOARD_AUTH_ENABLED": "false",
        "AUDIT_RETENTION_PURGE_MODE": "plan",
        "AUDIT_RETENTION_DAYS": "90",
        "OTEL_TRACE_UI_URL_TEMPLATE": "",
        "SANDBOX_BACKEND": "simulated",
        "SANDBOX_MAX_TIMEOUT_SECONDS": "300",
        "SANDBOX_MAX_OUTPUT_BYTES": "1048576",
    }
    base.update(values)
    for key, value in base.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()


def _seed_old(session_factory, *run_ids, tenant="t1"):
    async def scenario():
        async with session_factory() as session:
            for run_id in run_ids:
                _add_run(session, run_id, tenant=tenant)
            await session.commit()

    asyncio.run(scenario())


def _seed_live_run(session_factory, run_id="run_live_dash"):
    transcript = [
        {
            "tool_name": "read_repo",
            "status": "allowed",
            "detail": "simulated read",
            "sandbox_execution": {
                "backend_id": "simulated",
                "isolation_level": "simulated",
                "status": "ok",
                "spec_digest": SPEC_DIGEST,
                "output_sha256": "f" * 64,
                "required_isolation": "gvisor",
                "isolation_satisfied": "n/a",
            },
        },
        {"tool_name": "deploy", "status": "blocked", "detail": "not allowed"},
    ]

    async def scenario():
        async with session_factory() as session:
            session.add(
                AgentRun(
                    run_id=run_id,
                    tenant_id="t1",
                    user_id="u1",
                    task_type="code_fix",
                    prompt="p",
                    goal="g",
                    status="completed",
                    decision_reason="ok",
                    request_payload={"metadata": {"trace_id": f"trace_{TRACE_HEX}"}},
                    evaluation_summary={"execution_event": {"payload": {"tool_transcript": transcript}}},
                )
            )
            await session.commit()

    asyncio.run(scenario())


def test_v160_retention_page_renders_policy_and_has_no_apply_or_release(client, monkeypatch):
    _env(monkeypatch)
    page = client.get("/dashboard/retention")
    assert page.status_code == 200
    html = page.text
    assert "Retention" in html and 'id="no-delete-banner"' in html and "本環境不會刪除資料" in html
    actions = re.findall(r'<form[^>]*action="([^"]+)"', html)
    assert actions == ["/dashboard/retention/plans", "/dashboard/retention/holds"]
    assert "/apply" not in "".join(actions) and "release" not in "".join(actions)
    assert 'href="/dashboard/retention"' in html  # navigation entry
    assert "0E-8" not in html


def test_v160_retention_dashboard_routes_exclude_apply_and_release():
    paths = {
        (route.path, method)
        for route in app.routes
        if getattr(route, "path", "").startswith("/dashboard/retention")
        for method in getattr(route, "methods", ())
    }
    assert paths == {
        ("/dashboard/retention", "GET"),
        ("/dashboard/retention/plans", "POST"),
        ("/dashboard/retention/holds", "POST"),
    }


def test_v160_dashboard_dry_run_plan_lists_candidates_and_api_command(client, monkeypatch, session_factory):
    _env(monkeypatch)
    _seed_old(session_factory, "run_dash_old")
    created = client.post("/dashboard/retention/plans", headers=SAME_ORIGIN, follow_redirects=False)
    assert created.status_code == 303 and "purge_id=purge_" in created.headers["location"]
    html = client.get(created.headers["location"]).text
    purge = client.get("/v1/retention/purges").json()[0]
    assert purge["status"] == "planned" and purge["candidate_run_ids"] == ["run_dash_old"]
    assert purge["purge_id"] in html and purge["plan_digest"] in html
    assert f"/v1/retention/plans/{purge['purge_id']}/apply" in html and 'id="apply-api-only"' in html
    # Still present: the Dashboard never deletes.
    assert client.get("/v1/runs/run_dash_old").status_code == 200


def test_v160_dashboard_plan_mode_off_shows_error(client, monkeypatch):
    _env(monkeypatch, AUDIT_RETENTION_PURGE_MODE="off")
    response = client.post("/dashboard/retention/plans", headers=SAME_ORIGIN)
    assert response.status_code == 200 and "retention_disabled" in response.text


def test_v160_dashboard_retention_forms_refuse_cross_origin(client, monkeypatch):
    _env(monkeypatch)
    for headers in ({}, {"Origin": "http://evil.example"}, {"Origin": "null"}):
        assert client.post("/dashboard/retention/plans", headers=headers).status_code == 403
        hold = client.post(
            "/dashboard/retention/holds", headers=headers, data={"scope_type": "tenant", "scope_id": "t1", "reason": "x"}
        )
        assert hold.status_code == 403
    assert client.get("/v1/retention/purges").json() == []
    assert client.get("/v1/legal-holds").json() == []


def test_v160_dashboard_creates_hold_and_lists_tabs(client, monkeypatch):
    _env(monkeypatch)
    created = client.post(
        "/dashboard/retention/holds",
        headers=SAME_ORIGIN,
        data={"scope_type": "tenant", "scope_id": "dash-tenant", "reason": "litigation"},
        follow_redirects=False,
    )
    assert created.status_code == 303
    hold = client.get("/v1/legal-holds").json()[0]
    assert hold["scope_id"] == "dash-tenant" and hold["created_by"] == "dashboard"
    active = client.get("/dashboard/retention?holds=active").text
    assert hold["hold_id"] in active and "Use the API (admin required)" in active
    assert hold["hold_id"] not in client.get("/dashboard/retention?holds=released").text
    missing = client.post(
        "/dashboard/retention/holds", headers=SAME_ORIGIN, data={"scope_type": "tenant", "scope_id": "t1", "reason": " "}
    )
    assert missing.status_code == 200 and "legal_hold_reason_required" in missing.text


def test_v160_run_detail_shows_tombstone_for_purged_run(client, monkeypatch, session_factory):
    _env(monkeypatch, AUDIT_RETENTION_PURGE_MODE="apply")
    _seed_old(session_factory, "run_dash_purged")
    plan = client.post("/v1/retention/plans").json()
    applied = client.post(
        f"/v1/retention/plans/{plan['purge_id']}/apply", json={"plan_digest": plan["plan_digest"], "confirm": True}
    )
    assert applied.status_code == 200
    page = client.get("/dashboard/runs/run_dash_purged")
    assert page.status_code == 200
    assert 'id="run-purged-notice"' in page.text and plan["purge_id"] in page.text
    tombstone = client.get("/v1/retention/tombstones").json()[0]
    assert tombstone["content_digest"] in page.text and "PROMPT-CONTENT" not in page.text
    assert "run_dash_purged" in client.get("/dashboard/retention").text  # tombstone list
    assert client.get("/dashboard/runs/run_never_existed").status_code == 404


def test_v160_run_detail_shows_trace_link_hold_and_sandbox_execution(client, monkeypatch, session_factory):
    _env(monkeypatch, OTEL_TRACE_UI_URL_TEMPLATE="http://localhost:16686/trace/{trace_id}")
    _seed_live_run(session_factory)
    html = client.get("/dashboard/runs/run_live_dash").text
    assert f"trace_{TRACE_HEX}" in html
    assert f'href="http://localhost:16686/trace/{TRACE_HEX}"' in html
    assert 'id="sandbox-execution"' in html and f"<code>{SPEC_DIGEST[:12]}</code>" in html
    assert "gvisor" in html and "n/a" in html
    assert 'id="run-hold-form"' in html and "Legal hold active" not in html
    client.post(
        "/dashboard/retention/holds",
        headers=SAME_ORIGIN,
        data={"scope_type": "run", "scope_id": "run_live_dash", "reason": "audit"},
    )
    assert "Legal hold active" in client.get("/dashboard/runs/run_live_dash").text


def test_v160_run_detail_has_no_trace_link_without_template(client, monkeypatch, session_factory):
    _env(monkeypatch)
    _seed_live_run(session_factory, "run_no_link")
    html = client.get("/dashboard/runs/run_no_link").text
    assert f"trace_{TRACE_HEX}" in html and "16686" not in html and "Open in Jaeger" not in html


def test_v160_observability_page_has_tracing_panel(client, monkeypatch):
    _env(monkeypatch)
    html = client.get("/dashboard/observability").text
    assert 'id="tracing"' in html and "W3C" in html and "Dropped attributes" in html


def test_v160_operations_page_lists_new_checks(client, monkeypatch):
    _env(monkeypatch)
    html = client.get("/dashboard/operations").text
    assert "retention_policy" in html and "sandbox_backend_health" in html
    assert "audit_retention_config" not in html


def test_v160_operations_and_observability_views_moved_to_ui_router():
    owners = {
        (route.path, method): route.endpoint.__module__
        for route in app.routes
        if hasattr(route, "methods")
        for method in route.methods
    }
    for key in (
        ("/dashboard/operations", "GET"),
        ("/dashboard/operations/evidence", "POST"),
        ("/dashboard/operations/release-gates", "POST"),
        ("/dashboard/observability", "GET"),
    ):
        assert owners.get(key) == ui_operations.__name__, key
    assert owners[("/dashboard/retention", "GET")] == ui_retention.__name__
