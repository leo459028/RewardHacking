from __future__ import annotations

from app.schemas.agent import AgentConstraints, AgentRequest
from app.services.control_plane import ControlPlanePlanner
from app.workers.consumer import process_run_by_id
from app.services.audit_log import persist_orchestration_result, get_run
from app.services.evaluator import Evaluator
from app.services.policy_engine import PolicyEngine
from app.services.orchestrator import AgentOrchestrator


def test_control_plane_planner_builds_execution_plan_for_allowed_tools():
    planner = ControlPlanePlanner()
    request = AgentRequest(
        tenant_id="default",
        user_id="u-v12",
        task_type="analysis",
        prompt="Analyze the incident trend.",
        goal="Summarize risk drivers",
        allowed_tools=["read_data", "summarize"],
        constraints=AgentConstraints(no_external_network=True, max_tool_calls=4),
    )

    plan = planner.plan(
        request=request,
        run_id="run-v12-plan",
        policy_snapshot={
            "decision": "allow",
            "reason": "Request passed deterministic policy checks.",
            "risk_score": 0.1,
            "flags": [],
            "requires_human_review": False,
        },
    )

    assert plan.run_id == "run-v12-plan"
    assert plan.workflow_id == "analysis-basic"
    assert plan.workflow_version == "1.0.0"
    assert plan.status == "planned"
    assert plan.model_route["model"] == "deterministic-control-plane"
    assert [tool["name"] for tool in plan.selected_tools] == ["read_data", "summarize"]
    assert [step["step_type"] for step in plan.steps] == ["policy_check", "tool_execution", "evaluation"]


def test_submit_details_persists_execution_plan_and_api_can_read_it(client, low_risk_payload):
    response = client.post("/v1/agent/submit-with-details", json=low_risk_payload)

    assert response.status_code == 200
    body = response.json()
    run_id = body["response"]["run_id"]
    plan_summary = body["evaluation_summary"]["execution_plan"]

    assert body["response"]["status"] == "queued"
    assert plan_summary["workflow_id"] == "analysis-basic"
    assert plan_summary["status"] == "planned"

    plan_response = client.get(f"/v1/runs/{run_id}/execution-plan")
    assert plan_response.status_code == 200
    plan = plan_response.json()
    assert plan["run_id"] == run_id
    assert plan["workflow_id"] == "analysis-basic"
    assert plan["policy_snapshot"]["decision"] == "allow"


def test_control_plane_registry_api_enforces_admin_for_writes(monkeypatch, client):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_KEYS", "viewer-key:default:viewer,admin-key:default:admin")
    from app.core.config import get_settings

    get_settings.cache_clear()
    try:
        viewer_create = client.post(
            "/v1/control-plane/workflows",
            headers={"X-API-Key": "viewer-key"},
            json={
                "workflow_id": "analysis-custom",
                "version": "1.0.0",
                "task_type": "analysis",
                "display_name": "Analysis Custom",
                "status": "active",
                "definition": {"steps": ["policy_check", "tool_execution", "evaluation"]},
            },
        )
        admin_create = client.post(
            "/v1/control-plane/workflows",
            headers={"X-API-Key": "admin-key"},
            json={
                "workflow_id": "analysis-custom",
                "version": "1.0.0",
                "task_type": "analysis",
                "display_name": "Analysis Custom",
                "status": "active",
                "definition": {"steps": ["policy_check", "tool_execution", "evaluation"]},
            },
        )
        viewer_list = client.get(
            "/v1/control-plane/workflows",
            headers={"X-API-Key": "viewer-key"},
        )
    finally:
        monkeypatch.delenv("AUTH_ENABLED", raising=False)
        monkeypatch.delenv("API_KEYS", raising=False)
        get_settings.cache_clear()

    assert viewer_create.status_code == 403
    assert admin_create.status_code == 200
    assert admin_create.json()["workflow_id"] == "analysis-custom"
    assert viewer_list.status_code == 200
    assert any(item["workflow_id"] == "analysis-custom" for item in viewer_list.json())


def test_worker_fails_closed_when_execution_plan_is_missing(session_factory):
    async def scenario():
        async with session_factory() as session:
            request = AgentRequest(
                tenant_id="default",
                user_id="u-worker-v12",
                task_type="analysis",
                prompt="Analyze the provided dataset carefully.",
                goal="Summarize the anomaly causes",
                allowed_tools=["read_data"],
                constraints=AgentConstraints(no_external_network=True, max_tool_calls=4),
            )
            result = AgentOrchestrator(
                policy_engine=PolicyEngine(),
                evaluator=Evaluator(),
            ).submit(request)
            await persist_orchestration_result(session, request, result)
            run_id = result.response.run_id
            updated = await process_run_by_id(
                session,
                run_id=run_id,
                state_store=__import__("app.services.run_state", fromlist=["InMemoryRunStateStore"]).InMemoryRunStateStore(),
                event_publisher=__import__("app.services.events", fromlist=["InMemoryEventPublisher"]).InMemoryEventPublisher(),
                max_attempts=1,
            )
            stored = await get_run(session, run_id)
            return updated, stored

    import asyncio

    updated, stored = asyncio.run(scenario())

    assert updated.status == "awaiting_approval"
    assert stored is not None
    assert "missing_execution_plan" in stored.policy_flags
    assert stored.evaluation_summary["control_plane"]["status"] == "missing_execution_plan"


def test_dashboard_shows_control_plane_and_run_execution_plan(client, low_risk_payload):
    response = client.post("/v1/agent/submit-with-details", json=low_risk_payload)
    run_id = response.json()["response"]["run_id"]

    overview = client.get("/dashboard/control-plane")
    detail = client.get(f"/dashboard/runs/{run_id}")

    assert overview.status_code == 200
    assert "Control Plane" in overview.text
    assert "analysis-basic" in overview.text
    assert detail.status_code == 200
    assert "Execution Plan" in detail.text
    assert "analysis-basic" in detail.text
