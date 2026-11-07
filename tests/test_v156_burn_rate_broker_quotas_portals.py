from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.models.agent_run import AgentRun
from app.models.approval_event import ApprovalEvent
from app.models.finops import CostEvent
from app.models.worker_attempt import WorkerAttempt
from app.services.finops import (
    FINOPS_CALCULATION_VERSION,
    compute_tenant_burn_rate_forecast,
)
from app.workers.consumer import process_run_by_id


def _run_worker(session_factory, run_id: str, client) -> None:
    async def _do():
        async with session_factory() as session:
            await process_run_by_id(
                session,
                run_id=run_id,
                state_store=client.state_store,
                event_publisher=client.event_publisher,
            )

    asyncio.run(_do())


def test_v156_burn_rate_forecast_and_exhaustion_warning(
    client, session_factory, low_risk_payload
):
    assert FINOPS_CALCULATION_VERSION.startswith("v1.5.")
    tenant_id = "burn-t-156"
    effective_start = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc).isoformat()

    # Create monthly tenant policy and monthly workflow policy to verify scope deduplication in budget_used_percent
    res_p1 = client.post(
        "/v1/budgets",
        json={
            "tenant_id": tenant_id,
            "policy_name": "Tenant Monthly Limit",
            "scope": "tenant",
            "scope_id": tenant_id,
            "period": "monthly",
            "limit_amount": "10.00",
            "currency": "USD",
            "warning_threshold_ratio": "0.80",
            "enforcement_mode": "warn",
            "effective_from": effective_start,
        },
    )
    assert res_p1.status_code == 200

    res_p2 = client.post(
        "/v1/budgets",
        json={
            "tenant_id": tenant_id,
            "policy_name": "Workflow Monthly Limit",
            "scope": "workflow",
            "scope_id": "analysis-default",
            "period": "monthly",
            "limit_amount": "50.00",
            "currency": "USD",
            "warning_threshold_ratio": "0.80",
            "enforcement_mode": "warn",
            "effective_from": effective_start,
        },
    )
    assert res_p2.status_code == 200

    # Insert 2 cost events inside the 24h window with high spend so projected period end exceeds limit
    fixed_now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)

    async def _seed_costs():
        async with session_factory() as session:
            for idx in range(2):
                session.add(
                    CostEvent(
                        cost_id=f"cost_burn_{idx}",
                        usage_event_id=f"usage_burn_{idx}",
                        run_id=f"run_burn_{idx}",
                        tenant_id=tenant_id,
                        workflow_id="analysis-default",
                        provider="internal",
                        model_name="deterministic-control-plane",
                        resource_type="model",
                        cost_type="actual",
                        quantity=Decimal("1000"),
                        usage_unit="output_tokens",
                        unit_price=Decimal("0.002"),
                        gross_cost=Decimal("2.00"),
                        discount_amount=Decimal("0"),
                        net_cost=Decimal("2.00"),
                        actual_cost=Decimal("2.00"),
                        currency="USD",
                        fx_rate=Decimal("1"),
                        fx_rate_source="identity",
                        tax_amount=Decimal("0"),
                        base_currency="USD",
                        base_currency_cost=Decimal("2.00"),
                        calculation_version="v1.5.6",
                        created_at=fixed_now - timedelta(hours=4 - idx),
                    )
                )
            await session.commit()

    asyncio.run(_seed_costs())

    async def _check_forecast():
        async with session_factory() as session:
            return await compute_tenant_burn_rate_forecast(
                session, tenant_id=tenant_id, now=fixed_now
            )

    forecast = asyncio.run(_check_forecast())
    assert forecast.tenant_id == tenant_id
    assert forecast.burn_status == "critical_burn"
    assert forecast.hourly_burn_rate > Decimal("0")
    assert forecast.daily_burn_rate > Decimal("0")
    assert forecast.projected_month_end_cost > Decimal("10.00")
    assert len(forecast.burn_rate_forecasts) >= 1
    tenant_pol = next(p for p in forecast.burn_rate_forecasts if p.scope == "tenant")
    assert tenant_pol.estimated_hours_to_hard_limit is not None
    assert tenant_pol.estimated_exhaustion_at is not None

    # Also test GET /v1/cost/tenants/{tenant_id}/forecast and summary endpoint
    api_forecast = client.get(f"/v1/cost/tenants/{tenant_id}/forecast")
    assert api_forecast.status_code == 200
    assert "0E-8" not in api_forecast.text

    # Seed two live cost events near real now so submit triggers finops_burn_rate_warning
    real_now = datetime.now(timezone.utc)

    async def _seed_live_costs():
        async with session_factory() as session:
            for idx in range(2):
                session.add(
                    CostEvent(
                        cost_id=f"cost_live_burn_{idx}",
                        usage_event_id=f"usage_live_burn_{idx}",
                        run_id=f"run_live_burn_{idx}",
                        tenant_id=tenant_id,
                        workflow_id="analysis-default",
                        provider="internal",
                        model_name="deterministic-control-plane",
                        resource_type="model",
                        cost_type="actual",
                        quantity=Decimal("1000"),
                        usage_unit="output_tokens",
                        unit_price=Decimal("0.003"),
                        gross_cost=Decimal("3.50"),
                        discount_amount=Decimal("0"),
                        net_cost=Decimal("3.50"),
                        actual_cost=Decimal("3.50"),
                        currency="USD",
                        fx_rate=Decimal("1"),
                        fx_rate_source="identity",
                        tax_amount=Decimal("0"),
                        base_currency="USD",
                        base_currency_cost=Decimal("3.50"),
                        calculation_version="v1.5.6",
                        created_at=real_now - timedelta(hours=2 - idx),
                    )
                )
            await session.commit()

    asyncio.run(_seed_live_costs())

    # Check tenant_cost_summary budget_used_percent only counts tenant-scoped limit ($10.00), not workflow ($50.00)
    summary_res = client.get(f"/v1/cost/tenants/{tenant_id}")
    assert summary_res.status_code == 200
    summary_data = summary_res.json()
    mtd = Decimal(summary_data["month_to_date_cost"])
    expected_pct = round(float((mtd / Decimal("10.00")) * Decimal("100")), 2)
    assert abs(float(summary_data["budget_used_percent"]) - expected_pct) < 0.05

    # Submit a run for this tenant and verify finops_burn_rate_warning is attached
    sub_res = client.post(
        "/v1/agent/submit",
        json={**low_risk_payload, "tenant_id": tenant_id},
    )
    assert sub_res.status_code == 200
    assert "finops_burn_rate_warning" in sub_res.json()["policy_flags"]


def test_v156_dynamic_token_broker_lease_release_and_settle_lifecycle(
    client, session_factory, low_risk_payload, high_risk_payload
):
    tenant_id = "broker-t-156"

    # 1. Policy-denied run must NEVER create estimated CostEvent or active lease
    denied_res = client.post(
        "/v1/agent/submit",
        json={
            **high_risk_payload,
            "tenant_id": tenant_id,
        },
    )
    assert denied_res.status_code == 200
    denied_run_id = denied_res.json()["run_id"]
    assert denied_res.json()["status"] == "denied"

    denied_cost = client.get(f"/v1/cost/runs/{denied_run_id}").json()
    assert Decimal(denied_cost["estimated_cost"]) == Decimal("0")
    assert Decimal(denied_cost["settled_cost"]) == Decimal("0")
    assert denied_cost["token_lease_status"].startswith("none")

    # 2. Budget fail_closed run must release lease immediately and settle to $0
    fc_pol = client.post(
        "/v1/budgets",
        json={
            "tenant_id": f"{tenant_id}-fc",
            "policy_name": "Tiny Fail Closed Run Limit",
            "scope": "tenant",
            "scope_id": f"{tenant_id}-fc",
            "period": "per_run",
            "limit_amount": "0.000001",
            "currency": "USD",
            "warning_threshold_ratio": "0.50",
            "enforcement_mode": "fail_closed",
        },
    )
    assert fc_pol.status_code == 200
    fc_res = client.post(
        "/v1/agent/submit",
        json={**low_risk_payload, "tenant_id": f"{tenant_id}-fc"},
    )
    assert fc_res.status_code == 200
    fc_data = fc_res.json()
    assert fc_data["status"] == "denied"
    fc_cost = client.get(f"/v1/cost/runs/{fc_data['run_id']}").json()
    assert fc_cost["token_lease_status"] == "released"
    assert Decimal(fc_cost["settled_cost"]) == Decimal("0")
    fc_tenant_summary = client.get(f"/v1/cost/tenants/{tenant_id}-fc").json()
    assert Decimal(fc_tenant_summary["month_to_date_cost"]) == Decimal("0")
    assert fc_tenant_summary["active_lease_count"] == 0

    # 3. Awaiting approval run holds active lease; denying approval releases lease and zeroes settled cost
    approval_payload = {
        **low_risk_payload,
        "tenant_id": tenant_id,
        "task_type": "workflow",
        "allowed_tools": ["deploy_service"],
    }
    app_res = client.post("/v1/agent/submit", json=approval_payload)
    assert app_res.status_code == 200
    app_run_id = app_res.json()["run_id"]
    assert app_res.json()["status"] == "awaiting_approval"

    leases_before = client.get(f"/v1/cost/tenants/{tenant_id}/leases").json()
    assert leases_before["active_lease_count"] == 1
    assert leases_before["active_leases"][0]["run_id"] == app_run_id

    # Deny approval
    dec_res = client.post(
        f"/v1/approvals/{app_run_id}/decision",
        json={
            "decision": "deny",
            "reviewer": "sec-reviewer",
            "reason": "Rejected in V1.5.6 broker test",
        },
    )
    assert dec_res.status_code == 200
    leases_after_deny = client.get(f"/v1/cost/tenants/{tenant_id}/leases").json()
    assert leases_after_deny["active_lease_count"] == 0
    app_cost_after = client.get(f"/v1/cost/runs/{app_run_id}").json()
    assert app_cost_after["token_lease_status"] == "released"
    assert Decimal(app_cost_after["settled_cost"]) == Decimal("0")

    # 4. Queued run holds active lease; worker completion settles lease
    q_res = client.post(
        "/v1/agent/submit",
        json={**low_risk_payload, "tenant_id": tenant_id},
    )
    assert q_res.status_code == 200
    q_run_id = q_res.json()["run_id"]
    assert q_res.json()["status"] == "queued"

    q_cost_before = client.get(f"/v1/cost/runs/{q_run_id}").json()
    assert q_cost_before["token_lease_status"] == "active"
    assert q_cost_before["token_lease_id"] == f"lease_{q_run_id}"

    _run_worker(session_factory, q_run_id, client)

    q_cost_after = client.get(f"/v1/cost/runs/{q_run_id}").json()
    assert q_cost_after["token_lease_status"] == "settled"
    assert Decimal(q_cost_after["settled_cost"]) > Decimal("0")
    leases_final = client.get(f"/v1/cost/tenants/{tenant_id}/leases").json()
    assert leases_final["active_lease_count"] == 0


def test_v156_approval_expire_stale_and_dlq_resolve_release_leases(
    client, session_factory, low_risk_payload
):
    tenant_id = "stale-dlq-156"

    # 1. Submit awaiting_approval run, force approval expiration in DB, call expire-stale
    app_res = client.post(
        "/v1/agent/submit",
        json={
            **low_risk_payload,
            "tenant_id": tenant_id,
            "task_type": "workflow",
            "allowed_tools": ["deploy_service"],
        },
    )
    assert app_res.status_code == 200
    run1_id = app_res.json()["run_id"]
    assert app_res.json()["status"] == "awaiting_approval"

    async def _expire_event():
        async with session_factory() as session:
            ev = await session.scalar(
                select(ApprovalEvent).where(ApprovalEvent.run_id == run1_id)
            )
            ev.expires_at = datetime.now(timezone.utc) - timedelta(minutes=10)
            await session.commit()

    asyncio.run(_expire_event())

    exp_res = client.post("/v1/approvals/expire-stale")
    assert exp_res.status_code == 200
    assert run1_id in exp_res.json()["run_ids"]

    c1 = client.get(f"/v1/cost/runs/{run1_id}").json()
    assert c1["token_lease_status"] == "released"
    assert Decimal(c1["settled_cost"]) == Decimal("0")

    # 2. Submit queued run, simulate DLQ attempt without actual cost, call dlq resolve -> releases lease
    q_res = client.post(
        "/v1/agent/submit",
        json={**low_risk_payload, "tenant_id": tenant_id},
    )
    run2_id = q_res.json()["run_id"]

    async def _make_dlq():
        async with session_factory() as session:
            run = await session.scalar(select(AgentRun).where(AgentRun.run_id == run2_id))
            run.status = "failed"
            session.add(
                WorkerAttempt(
                    run_id=run2_id,
                    attempt_number=1,
                    status="dlq",
                    failure_reason="simulated dlq",
                )
            )
            await session.commit()

    asyncio.run(_make_dlq())

    dlq_res = client.post(f"/v1/worker/dlq/{run2_id}/resolve")
    assert dlq_res.status_code == 200
    c2 = client.get(f"/v1/cost/runs/{run2_id}").json()
    assert c2["token_lease_status"] == "released"
    assert Decimal(c2["settled_cost"]) == Decimal("0")


def test_v156_resume_and_requeue_reacquire_leases_and_running_release_409(
    client, session_factory, low_risk_payload
):
    tenant_id = "resume-156"
    sub_res = client.post(
        "/v1/agent/submit",
        json={**low_risk_payload, "tenant_id": tenant_id},
    )
    run_id = sub_res.json()["run_id"]

    # Manually release lease first
    rel_res = client.post(
        f"/v1/cost/leases/{run_id}/release",
        json={"reason": "Manual test release"},
    )
    assert rel_res.status_code == 200
    assert rel_res.json()["status"] == "released"
    assert Decimal(client.get(f"/v1/cost/runs/{run_id}").json()["settled_cost"]) == Decimal("0")

    # Resume from checkpoint re-acquires lease
    res_resume = client.post(
        f"/v1/runs/{run_id}/resume",
        json={"resume_cursor": "worker_queue", "resume_reason": "Re-acquire lease on resume"},
    )
    assert res_resume.status_code == 200
    after_resume_cost = client.get(f"/v1/cost/runs/{run_id}").json()
    assert after_resume_cost["token_lease_status"] == "active"
    assert Decimal(after_resume_cost["settled_cost"]) > Decimal("0")

    # Set run status to running and verify manual release returns 409 Conflict
    async def _set_running():
        async with session_factory() as session:
            run = await session.scalar(select(AgentRun).where(AgentRun.run_id == run_id))
            run.status = "running"
            await session.commit()

    asyncio.run(_set_running())

    conflict_res = client.post(
        f"/v1/cost/leases/{run_id}/release",
        json={"reason": "Should fail while running"},
    )
    assert conflict_res.status_code == 409


def test_v156_user_request_quota_and_tool_call_quota_enforcement(
    client, low_risk_payload, monkeypatch
):
    from app.core.config import get_settings

    monkeypatch.setenv("QUOTA_ENABLED", "true")
    get_settings.cache_clear()
    tenant_id = "quota-156"
    # Register a tool with quota_per_minute = 2
    t_res = client.post(
        "/v1/control-plane/tools",
        json={
            "tool_name": "rate_limited_tool",
            "display_name": "Rate Limited Tool",
            "description": "Tool with strict per-minute quota",
            "task_types": ["analysis"],
            "risk_level": "low",
            "requires_approval": False,
            "status": "active",
            "metadata": {"quota_per_minute": 2},
        },
    )
    assert t_res.status_code == 200

    payload = {
        **low_risk_payload,
        "tenant_id": tenant_id,
        "user_id": "quota-user-1",
        "allowed_tools": ["rate_limited_tool"],
    }
    assert client.post("/v1/agent/submit", json=payload).status_code == 200
    assert client.post("/v1/agent/submit", json=payload).status_code == 200
    # 3rd call hits tool quota (429)
    r3 = client.post("/v1/agent/submit", json=payload)
    assert r3.status_code == 429
    err_msg = r3.json()["error"]["message"]
    assert "rate_limited_tool" in err_msg and "quota exceeded" in err_msg.lower()

    gov_summary = client.get(f"/v1/governance/summary?tenant_id={tenant_id}").json()
    assert gov_summary["quota_limited"] >= 1


def test_v156_playground_respects_tenant_user_and_tool_quotas(client, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setenv("QUOTA_ENABLED", "true")
    get_settings.cache_clear()
    tenant_id = "pg-quota-156"
    t_res = client.post(
        "/v1/tenants",
        json={
            "tenant_id": tenant_id,
            "display_name": "PG Quota Tenant",
            "request_limit_per_minute": 1,
            "high_risk_limit_per_hour": 5,
        },
    )
    assert t_res.status_code == 200
    form_data = {
        "tenant_id": tenant_id,
        "user_id": "pg-user",
        "task_type": "analysis",
        "prompt": "Summarize quarterly safety metrics.",
        "goal": "Summarize metrics",
        "allowed_tools": "read_data",
        "execution_mode": "sync",
    }
    r1 = client.post("/dashboard/playground", data=form_data)
    assert r1.status_code == 200
    r2 = client.post("/dashboard/playground", data=form_data)
    # Renders 200 with inline quota error message in playground
    assert r2.status_code == 200
    assert "quota exceeded" in r2.text.lower()


def test_v156_tool_breaker_manual_and_worker_auto_trip_close_loop(
    client, session_factory, low_risk_payload
):
    # 1. Manual trip & close via REST API
    open_res = client.post(
        "/v1/control-plane/tools/search_docs/breaker/open",
        json={"reason": "Upstream search maintenance"},
    )
    assert open_res.status_code == 200
    assert open_res.json()["is_open"] is True
    assert open_res.json()["status"] == "open"

    list_res = client.get("/v1/control-plane/breakers")
    assert list_res.status_code == 200
    assert any(b["tool_name"] == "search_docs" for b in list_res.json())

    # Submitting a run with search_docs while breaker is OPEN escalates to awaiting_approval with tool_circuit_breaker_open:search_docs
    sub_res = client.post(
        "/v1/agent/submit",
        json={
            **low_risk_payload,
            "tenant_id": "breaker-t-156",
            "allowed_tools": ["search_docs"],
        },
    )
    assert sub_res.status_code == 200
    assert sub_res.json()["status"] == "awaiting_approval"
    assert "tool_circuit_breaker_open:search_docs" in sub_res.json()["policy_flags"]

    # Close breaker
    close_res = client.post(
        "/v1/control-plane/tools/search_docs/breaker/close",
        json={"reason": "Maintenance finished"},
    )
    assert close_res.status_code == 200
    assert close_res.json()["is_open"] is False
    assert close_res.json()["status"] == "closed"

    # 2. Worker consecutive failures auto-trip breaker
    for _ in range(3):
        fail_sub = client.post(
            "/v1/agent/submit",
            json={
                **low_risk_payload,
                "tenant_id": "breaker-auto-156",
                "allowed_tools": ["read_data"],
                "metadata": {"simulate_worker_failure": True},
            },
        )
        assert fail_sub.json()["status"] == "queued"
        run_id = fail_sub.json()["run_id"]
        _run_worker(session_factory, run_id, client)

    breakers_after = client.get("/v1/control-plane/breakers").json()
    assert any(b["tool_name"] == "read_data" for b in breakers_after)

    # Clean up read_data breaker
    client.post(
        "/v1/control-plane/tools/read_data/breaker/close",
        json={"reason": "Reset after test"},
    )


def test_v156_dashboard_governance_and_control_plane_crud_portals(client):
    # 1. Governance Portal: Create Tenant, Issue API Key (200 + Cache-Control: no-store), Revoke API Key
    r_tenant = client.post(
        "/dashboard/governance/tenants",
        data={
            "tenant_id": "v156-portal-t",
            "display_name": "V1.5.6 Portal Tenant",
            "status": "active",
            "request_limit_per_minute": "45",
            "high_risk_limit_per_hour": "8",
        },
        follow_redirects=True,
    )
    assert r_tenant.status_code == 200
    assert "V1.5.6 Portal Tenant" in r_tenant.text

    r_key = client.post(
        "/dashboard/governance/api-keys",
        data={
            "tenant_id": "v156-portal-t",
            "name": "portal-test-key",
            "role": "operator",
        },
    )
    assert r_key.status_code == 200
    assert r_key.headers.get("Cache-Control") == "no-store"
    assert "One-Time API Key Issued" in r_key.text
    assert "rg_" in r_key.text

    keys = client.get("/v1/tenants/v156-portal-t/api-keys").json()
    assert len(keys) == 1
    key_id = keys[0]["key_id"]

    r_revoke = client.post(
        f"/dashboard/governance/tenants/v156-portal-t/api-keys/{key_id}/revoke",
        follow_redirects=True,
    )
    assert r_revoke.status_code == 200
    keys_after = client.get("/v1/tenants/v156-portal-t/api-keys").json()
    assert keys_after[0]["revoked"] is True

    # 2. Control Plane Portal: Register Workflow, Register Tool, Trip & Reset Breaker
    r_wf = client.post(
        "/dashboard/control-plane/workflows",
        data={
            "workflow_id": "wf-v156-portal",
            "version": "v1",
            "task_type": "analysis",
            "display_name": "Portal Workflow V156",
            "status": "active",
            "steps": "sanitize_input,policy_evaluation,execute_tools",
        },
        follow_redirects=True,
    )
    assert r_wf.status_code == 200
    assert "wf-v156-portal" in r_wf.text

    r_tool = client.post(
        "/dashboard/control-plane/tools",
        data={
            "tool_name": "portal_custom_tool",
            "display_name": "Portal Custom Tool",
            "task_types": "analysis,data_query",
            "risk_level": "low",
            "quota_per_minute": "25",
            "requires_approval": "",
        },
        follow_redirects=True,
    )
    assert r_tool.status_code == 200
    assert "portal_custom_tool" in r_tool.text

    r_trip = client.post(
        "/dashboard/control-plane/tools/portal_custom_tool/breaker/open",
        data={"reason": "Trip from UI test"},
        follow_redirects=True,
    )
    assert r_trip.status_code == 200
    assert "OPEN" in r_trip.text

    r_reset = client.post(
        "/dashboard/control-plane/tools/portal_custom_tool/breaker/close",
        data={"reason": "Reset from UI test"},
        follow_redirects=True,
    )
    assert r_reset.status_code == 200


def test_v156_operations_readiness_includes_burn_rate_check(client):
    res = client.get("/v1/operations/readiness")
    assert res.status_code == 200
    checks = res.json()["checks"]
    burn_check = next(
        (c for c in checks if c["check_key"] == "finops_burn_rate_health"), None
    )
    assert burn_check is not None
    assert burn_check["status"] in ("pass", "warn")

    # Verify config validation rejects finops_token_lease_ttl_seconds < approval_ttl_seconds
    with pytest.raises(ValueError, match="FINOPS_TOKEN_LEASE_TTL_SECONDS"):
        Settings(
            approval_ttl_seconds=3600,
            finops_token_lease_ttl_seconds=1800,
        )
