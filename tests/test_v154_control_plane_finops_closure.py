from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.models.approval_event import ApprovalEvent
from app.services.orchestrator import AgentOrchestrator
from app.workers.consumer import process_run_by_id


class _FailingOrchestrator(AgentOrchestrator):
    def execute(self, request, *, run_id=None, disabled_tools=None):
        raise RuntimeError("Simulated tool runtime failure for v1.5.4 test")


def test_v154_model_aware_submit_and_worker_pricing(client, session_factory):
    # Apply official pricing seed so openai/gpt-5.4 is active in catalog
    imp = client.post(
        "/v1/admin/finops/pricing/import/apply",
        json={"file_path": "app/seeds/model_pricing_seed.yaml", "allow_warnings": True},
    )
    assert imp.status_code == 200
    assert imp.json()["status"] == "applied"

    payload = {
        "tenant_id": "v154-model-tenant",
        "user_id": "eng-01",
        "task_type": "code_fix",
        "prompt": "Inspect retry helper and verify tests pass cleanly.",
        "goal": "Fix retry backoff bug",
        "allowed_tools": ["read_repo", "run_tests"],
        "constraints": {
            "no_external_network": True,
            "requires_human_approval": False,
            "max_tool_calls": 4,
            "allow_destructive_actions": False,
            "allow_pii_access": False,
        },
        "metadata": {
            "provider": "openai",
            "model_name": "gpt-5.4",
            "pricing_mode": "standard",
            "project_id": "proj-v154",
        },
    }
    resp = client.post("/v1/agent/submit", json=payload)
    assert resp.status_code == 200
    run_id = resp.json()["run_id"]

    # Process run via worker
    async def _run_worker():
        async with session_factory() as session:
            await process_run_by_id(
                session,
                run_id=run_id,
                state_store=client.state_store,
                event_publisher=client.event_publisher,
            )

    asyncio.run(_run_worker())

    trace_resp = client.get(f"/v1/runs/{run_id}/trace")
    assert trace_resp.status_code == 200
    model_route = trace_resp.json()["execution_plan"]["model_route"]
    assert model_route["provider"] == "openai"
    assert model_route["model"] == "gpt-5.4"
    assert model_route["project_id"] == "proj-v154"

    cost_resp = client.get(f"/v1/cost/runs/{run_id}")
    assert cost_resp.status_code == 200
    cost_data = cost_resp.json()
    assert "openai" in cost_data["provider_breakdown"]
    assert "gpt-5.4" in cost_data["model_breakdown"]
    assert Decimal(cost_data["estimated_cost"]) > Decimal("0")
    assert Decimal(cost_data["actual_cost"]) > Decimal("0")


def test_v154_user_project_model_scope_budget_isolation(client):
    # 1. User-scoped hard budget on alice
    b_user = client.post(
        "/v1/budgets",
        json={
            "policy_name": "Alice User Cap",
            "tenant_id": "v154-scope-tenant",
            "scope": "user",
            "scope_id": "alice",
            "period": "daily",
            "hard_limit_amount": "0.000001",
            "actions_on_hard_limit": ["record_violation", "fail_closed"],
        },
    )
    assert b_user.status_code == 200

    base_req = {
        "tenant_id": "v154-scope-tenant",
        "task_type": "analysis",
        "prompt": "Analyze quarterly operational metrics.",
        "goal": "Summarize metrics",
        "allowed_tools": ["read_data"],
        "constraints": {
            "no_external_network": True,
            "requires_human_approval": False,
            "max_tool_calls": 4,
            "allow_destructive_actions": False,
            "allow_pii_access": False,
        },
    }

    r_alice = client.post("/v1/agent/submit", json={**base_req, "user_id": "alice"})
    assert r_alice.status_code == 200
    assert r_alice.json()["status"] == "denied"
    assert "finops_budget_hard_limit_exceeded" in r_alice.json()["policy_flags"]

    r_bob = client.post("/v1/agent/submit", json={**base_req, "user_id": "bob"})
    assert r_bob.status_code == 200
    assert r_bob.json()["status"] == "queued"

    # 2. Project-scoped hard budget on proj-restricted
    b_proj = client.post(
        "/v1/budgets",
        json={
            "policy_name": "Restricted Project Cap",
            "tenant_id": "v154-scope-tenant",
            "scope": "project",
            "scope_id": "proj-restricted",
            "project_id": "proj-restricted",
            "period": "daily",
            "hard_limit_amount": "0.000001",
            "actions_on_hard_limit": ["record_violation", "fail_closed"],
        },
    )
    assert b_proj.status_code == 200

    r_proj_blocked = client.post(
        "/v1/agent/submit",
        json={**base_req, "user_id": "bob", "metadata": {"project_id": "proj-restricted"}},
    )
    assert r_proj_blocked.status_code == 200
    assert r_proj_blocked.json()["status"] == "denied"

    r_proj_ok = client.post(
        "/v1/agent/submit",
        json={**base_req, "user_id": "bob", "metadata": {"project_id": "proj-open"}},
    )
    assert r_proj_ok.status_code == 200
    assert r_proj_ok.json()["status"] == "queued"


def test_v154_budget_dry_run_only_enforcement(client):
    b_dry = client.post(
        "/v1/budgets",
        json={
            "policy_name": "Dry Run Cap",
            "tenant_id": "v154-dryrun-tenant",
            "scope": "tenant",
            "scope_id": "v154-dryrun-tenant",
            "period": "daily",
            "hard_limit_amount": "0.000001",
            "actions_on_hard_limit": ["record_violation", "dry_run_only"],
        },
    )
    assert b_dry.status_code == 200

    resp = client.post(
        "/v1/agent/submit",
        json={
            "tenant_id": "v154-dryrun-tenant",
            "user_id": "u-dry",
            "task_type": "analysis",
            "prompt": "Analyze dataset with dry run fallback.",
            "goal": "Summarize dataset",
            "allowed_tools": ["read_data"],
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "queued"
    assert "finops_budget_dry_run_only" in data["policy_flags"]

    run_id = data["run_id"]
    trace_resp = client.get(f"/v1/runs/{run_id}/trace")
    assert trace_resp.status_code == 200
    assert trace_resp.json()["execution_plan"]["policy_snapshot"].get("forced_dry_run") is True

    run_resp = client.get(f"/v1/runs/{run_id}")
    assert run_resp.status_code == 200
    assert run_resp.json()["request_payload"]["metadata"].get("forced_dry_run") is True


def test_v154_worker_failed_retry_cost_and_budget_exclusion(client, session_factory):
    resp = client.post(
        "/v1/agent/submit",
        json={
            "tenant_id": "v154-retry-tenant",
            "user_id": "u-retry",
            "task_type": "analysis",
            "prompt": "Run analysis that will fail on worker execution.",
            "goal": "Test failed_retry cost accounting",
            "allowed_tools": ["read_data"],
        },
    )
    assert resp.status_code == 200
    run_id = resp.json()["run_id"]

    async def _fail_worker():
        async with session_factory() as session:
            await process_run_by_id(
                session,
                run_id=run_id,
                state_store=client.state_store,
                event_publisher=client.event_publisher,
                orchestrator=_FailingOrchestrator(
                    policy_engine=None,  # type: ignore[arg-type]
                    evaluator=None,  # type: ignore[arg-type]
                ),
                max_attempts=1,
            )

    asyncio.run(_fail_worker())

    run_cost = client.get(f"/v1/cost/runs/{run_id}")
    assert run_cost.status_code == 200
    assert Decimal(run_cost.json()["failed_retry_cost"]) > Decimal("0")

    tenant_cost = client.get("/v1/cost/tenants/v154-retry-tenant")
    assert tenant_cost.status_code == 200
    assert Decimal(tenant_cost.json()["failed_run_cost"]) > Decimal("0")


def test_v154_step_audit_lifecycle_sync_on_worker_and_approval(client, session_factory):
    # 1. Low-risk run -> worker completion updates all steps to completed
    resp = client.post(
        "/v1/agent/submit",
        json={
            "tenant_id": "v154-step-tenant",
            "user_id": "u-step",
            "task_type": "code_fix",
            "prompt": "Check repository and run tests.",
            "goal": "Verify step audit lifecycle",
            "allowed_tools": ["read_repo", "run_tests"],
        },
    )
    assert resp.status_code == 200
    run_id = resp.json()["run_id"]

    async def _complete_worker(target_run_id: str):
        async with session_factory() as session:
            await process_run_by_id(
                session,
                run_id=target_run_id,
                state_store=client.state_store,
                event_publisher=client.event_publisher,
            )

    asyncio.run(_complete_worker(run_id))

    bundle_resp = client.get(f"/v1/runs/{run_id}/replay-bundle")
    assert bundle_resp.status_code == 200
    bundle = bundle_resp.json()
    assert bundle["execution_plan"]["status"] == "completed"
    steps = bundle["step_audit"]
    assert len(steps) == 3
    for step in steps:
        assert step["status"] == "completed"
        assert step["duration_ms"] is not None
        assert step["output_summary"]

    cp_html = client.get("/dashboard/control-plane")
    assert cp_html.status_code == 200
    assert "3/3 completed" in cp_html.text
    assert "healthy" in cp_html.text

    # 2. Approval-gated run -> approve_once updates approval-gate step to completed
    resp_app = client.post(
        "/v1/agent/submit",
        json={
            "tenant_id": "v154-step-tenant",
            "user_id": "u-step",
            "task_type": "workflow",
            "prompt": "Notify operator and deploy service under supervision.",
            "goal": "Controlled workflow deployment",
            "allowed_tools": ["notify_operator", "deploy_service"],
            "constraints": {
                "no_external_network": True,
                "requires_human_approval": False,
                "max_tool_calls": 4,
                "allow_destructive_actions": True,
                "allow_pii_access": False,
            },
        },
    )
    assert resp_app.status_code == 200
    app_run_id = resp_app.json()["run_id"]
    assert resp_app.json()["status"] == "awaiting_approval"

    dec_resp = client.post(
        f"/v1/approvals/{app_run_id}/decision",
        json={"decision": "approve_once", "reviewer": "qa-lead", "reason": "Approved for v1.5.4 test"},
    )
    assert dec_resp.status_code == 200
    assert dec_resp.json()["status"] == "queued"

    trace_after_app = client.get(f"/v1/runs/{app_run_id}/trace").json()
    assert trace_after_app["execution_plan"]["status"] == "planned"
    app_steps = {s["step_type"]: s for s in trace_after_app["steps"]}
    assert app_steps["approval_gate"]["status"] == "completed"
    assert "approve_once" in app_steps["approval_gate"]["output_summary"]


def test_v154_worker_dlq_requeue_and_resolve_clears_readiness(client, session_factory):
    resp = client.post(
        "/v1/agent/submit",
        json={
            "tenant_id": "v154-dlq-tenant",
            "user_id": "u-dlq",
            "task_type": "analysis",
            "prompt": "Trigger DLQ and test requeue/resolve.",
            "goal": "DLQ self-healing test",
            "allowed_tools": ["read_data"],
        },
    )
    run_id = resp.json()["run_id"]

    async def _fail_to_dlq():
        async with session_factory() as session:
            await process_run_by_id(
                session,
                run_id=run_id,
                state_store=client.state_store,
                event_publisher=client.event_publisher,
                orchestrator=_FailingOrchestrator(
                    policy_engine=None,  # type: ignore[arg-type]
                    evaluator=None,  # type: ignore[arg-type]
                ),
                max_attempts=1,
            )

    asyncio.run(_fail_to_dlq())

    dlq_list = client.get("/v1/worker/dlq").json()
    assert any(item["run_id"] == run_id for item in dlq_list)

    # Test Requeue API
    req_resp = client.post(f"/v1/worker/dlq/{run_id}/requeue")
    assert req_resp.status_code == 200
    assert req_resp.json()["status"] == "queued"
    assert "dlq_requeued" in req_resp.json()["policy_flags"]

    # Fail again to DLQ, then Resolve DLQ
    asyncio.run(_fail_to_dlq())
    res_resp = client.post(
        f"/v1/worker/dlq/{run_id}/resolve",
        json={"reason": "Resolved after root cause triage"},
    )
    assert res_resp.status_code == 200
    assert res_resp.json()[0]["status"] == "dlq_resolved"

    dlq_after = client.get("/v1/worker/dlq").json()
    assert not any(item["run_id"] == run_id for item in dlq_after)

    readiness = client.get("/v1/operations/readiness").json()
    dlq_check = next(c for c in readiness["checks"] if c["check_key"] == "worker_dlq")
    assert dlq_check["status"] == "pass"


def test_v154_expire_stale_approvals_sweep_and_readiness(client, session_factory):
    resp = client.post(
        "/v1/agent/submit",
        json={
            "tenant_id": "v154-ttl-tenant",
            "user_id": "u-ttl",
            "task_type": "workflow",
            "prompt": "Run requiring approval that will expire.",
            "goal": "Test stale approval sweep",
            "allowed_tools": ["notify_operator", "deploy_service"],
            "constraints": {
                "no_external_network": True,
                "requires_human_approval": False,
                "max_tool_calls": 4,
                "allow_destructive_actions": True,
                "allow_pii_access": False,
            },
        },
    )
    assert resp.status_code == 200
    run_id = resp.json()["run_id"]
    assert resp.json()["status"] == "awaiting_approval"

    # Force ApprovalEvent.expires_at into the past
    async def _backdate_approval():
        async with session_factory() as session:
            from sqlalchemy import select

            ev = await session.scalar(select(ApprovalEvent).where(ApprovalEvent.run_id == run_id))
            assert ev is not None
            ev.expires_at = datetime.now(timezone.utc) - timedelta(hours=2)
            await session.commit()

    asyncio.run(_backdate_approval())

    readiness_before = client.get("/v1/operations/readiness").json()
    exp_check_before = next(c for c in readiness_before["checks"] if c["check_key"] == "expired_approvals")
    assert exp_check_before["status"] == "warn"

    sweep_resp = client.post("/v1/approvals/expire-stale")
    assert sweep_resp.status_code == 200
    assert run_id in sweep_resp.json()["run_ids"]

    run_after = client.get(f"/v1/runs/{run_id}").json()
    assert run_after["status"] == "denied"
    assert "approval_ttl_expired" in run_after["policy_flags"]

    readiness_after = client.get("/v1/operations/readiness").json()
    exp_check_after = next(c for c in readiness_after["checks"] if c["check_key"] == "expired_approvals")
    assert exp_check_after["status"] == "pass"


def test_v154_dashboard_run_detail_replay_resume_and_playground_model(client):
    pg_resp = client.post(
        "/dashboard/playground",
        data={
            "tenant_id": "v154-ui-tenant",
            "user_id": "ui-tester",
            "task_type": "code_fix",
            "allowed_tools": "read_repo, run_tests",
            "max_tool_calls": "6",
            "provider": "openai",
            "model_name": "gpt-5.4",
            "pricing_mode": "standard",
            "project_id": "proj-ui-v154",
            "goal": "Test playground model routing",
            "prompt": "Inspect repository and run tests.",
            "no_external_network": "on",
        },
    )
    assert pg_resp.status_code == 200
    runs = client.get("/v1/runs?tenant_id=v154-ui-tenant").json()
    assert len(runs) == 1
    run_id = runs[0]["run_id"]

    # Trigger Dry-Run Replay via Dashboard UI route
    rep_ui = client.post(f"/dashboard/runs/{run_id}/replay")
    assert rep_ui.status_code == 200

    # Trigger Resume from Checkpoint via Dashboard UI route
    res_ui = client.post(
        f"/dashboard/runs/{run_id}/resume",
        data={"resume_reason": "UI checkpoint resume test", "resume_cursor": "worker_queue"},
    )
    assert res_ui.status_code == 200

    detail_html = client.get(f"/dashboard/runs/{run_id}")
    assert detail_html.status_code == 200
    assert "gpt-5.4" in detail_html.text
    assert "replay_" in detail_html.text
    assert "ckpt_" in detail_html.text
