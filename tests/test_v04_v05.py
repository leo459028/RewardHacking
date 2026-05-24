import asyncio

from app.workers.consumer import process_run_by_id


class AlwaysFailingOrchestrator:
    def execute(self, request, *, run_id, disabled_tools):
        raise RuntimeError("transient worker failure")


def test_worker_attempts_tool_executions_and_observability(client, low_risk_payload, session_factory):
    response = client.post("/v1/agent/submit", json=low_risk_payload)
    run_id = response.json()["run_id"]

    async def run_worker_once():
        async with session_factory() as session:
            await process_run_by_id(
                session,
                run_id=run_id,
                state_store=client.state_store,
                event_publisher=client.event_publisher,
            )

    asyncio.run(run_worker_once())

    attempts = client.get(f"/v1/runs/{run_id}/worker-attempts")
    assert attempts.status_code == 200
    assert attempts.json()[0]["attempt_number"] == 1
    assert attempts.json()[0]["status"] == "completed"

    tool_executions = client.get(f"/v1/runs/{run_id}/tool-executions")
    assert tool_executions.status_code == 200
    assert tool_executions.json()[0]["tool_name"] == "read_data"
    assert tool_executions.json()[0]["status"] == "simulated"
    assert "sandbox_policy" in tool_executions.json()[0]

    observability = client.get("/v1/observability/summary")
    assert observability.status_code == 200
    assert observability.json()["worker"]["completed"] == 1
    assert observability.json()["slo"]["worker_completion_latency_target_seconds"] == 300


def test_worker_retry_exhaustion_creates_dlq_entry(client, low_risk_payload, session_factory):
    response = client.post("/v1/agent/submit", json=low_risk_payload)
    run_id = response.json()["run_id"]

    async def run_worker_attempt():
        async with session_factory() as session:
            await process_run_by_id(
                session,
                run_id=run_id,
                state_store=client.state_store,
                event_publisher=client.event_publisher,
                orchestrator=AlwaysFailingOrchestrator(),
                max_attempts=3,
            )

    asyncio.run(run_worker_attempt())
    asyncio.run(run_worker_attempt())
    asyncio.run(run_worker_attempt())

    detail = client.get(f"/v1/runs/{run_id}")
    assert detail.json()["status"] == "failed"
    assert "Retry exhausted" in detail.json()["decision_reason"]

    dlq = client.get("/v1/worker/dlq")
    assert dlq.status_code == 200
    assert dlq.json()[0]["run_id"] == run_id
    assert dlq.json()[0]["status"] == "dlq"
    assert dlq.json()[0]["failure_reason"] == "transient worker failure"


def test_approval_filters_ttl_and_request_changes_event(client, low_risk_payload):
    payload = {
        **low_risk_payload,
        "task_type": "workflow",
        "allowed_tools": ["deploy_service"],
        "constraints": {
            **low_risk_payload["constraints"],
            "allow_destructive_actions": True,
            "requires_human_approval": False,
        },
        "metadata": {"approval_ttl_seconds": -1},
    }
    response = client.post("/v1/agent/submit-with-details", json=payload)
    run_id = response.json()["response"]["run_id"]

    expired = client.get("/v1/approvals?expired=true")
    assert expired.status_code == 200
    assert expired.json()[0]["run_id"] == run_id

    decision = client.post(
        f"/v1/approvals/{run_id}/decision",
        json={
            "decision": "request_changes",
            "reviewer": "security-reviewer",
            "reason": "Need a rollback plan before approval.",
        },
    )
    assert decision.status_code == 200

    by_reviewer = client.get("/v1/approvals?reviewer=security-reviewer")
    assert by_reviewer.status_code == 200
    assert by_reviewer.json()[0]["run_id"] == run_id
    assert by_reviewer.json()[0]["evaluation_summary"]["approval"]["status"] == "request_changes"


def test_evaluation_results_and_dashboard_pages(client, low_risk_payload, session_factory):
    payload = {
        **low_risk_payload,
        "task_type": "code_fix",
        "allowed_tools": ["read_repo", "run_tests"],
        "metadata": {
            "diff_summary": "Changed payment validation branch.",
            "test_result": "pytest passed",
            "lint_result": "ruff passed",
        },
    }
    response = client.post("/v1/agent/submit", json=payload)
    run_id = response.json()["run_id"]

    async def run_worker_once():
        async with session_factory() as session:
            await process_run_by_id(
                session,
                run_id=run_id,
                state_store=client.state_store,
                event_publisher=client.event_publisher,
            )

    asyncio.run(run_worker_once())

    evaluations = client.get("/v1/evaluations?task_type=code_fix&verdict=pass")
    assert evaluations.status_code == 200
    assert evaluations.json()[0]["run_id"] == run_id
    assert evaluations.json()[0]["rubric"]["task_type"] == "code_fix"
    assert evaluations.json()[0]["judge_signal"]["enabled"] is False

    worker_page = client.get("/dashboard/worker")
    observability_page = client.get("/dashboard/observability")
    assert worker_page.status_code == 200
    assert "Worker Operations" in worker_page.text
    assert observability_page.status_code == 200
    assert "Observability Trends" in observability_page.text
