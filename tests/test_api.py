import asyncio

from app.workers.consumer import process_run_by_id


def test_healthz_and_readyz(client):
    health = client.get("/v1/healthz")
    ready = client.get("/v1/readyz")

    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready"}


def test_submit_low_risk_request_queues_run_and_publishes_request(client, low_risk_payload, session_factory):
    response = client.post("/v1/agent/submit", json=low_risk_payload)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "queued"

    runs = client.get("/v1/runs")
    assert runs.status_code == 200
    assert len(runs.json()) == 1
    assert runs.json()[0]["run_id"] == body["run_id"]
    assert runs.json()[0]["requires_human_review"] is False
    assert runs.json()[0]["evaluation_summary"]["run_state"]["status"] == "queued"
    assert client.event_publisher.events[0]["topic"] == "agent.requests"

    detail = client.get(f"/v1/runs/{body['run_id']}")
    assert detail.status_code == 200
    assert detail.json()["execution_output"] == ""

    async def run_worker_once():
        async with session_factory() as session:
            await process_run_by_id(
                session,
                run_id=body["run_id"],
                state_store=client.state_store,
                event_publisher=client.event_publisher,
            )

    asyncio.run(run_worker_once())

    detail_after_worker = client.get(f"/v1/runs/{body['run_id']}")
    assert detail_after_worker.status_code == 200
    worker_body = detail_after_worker.json()
    assert worker_body["status"] == "completed"
    assert worker_body["execution_output"]
    assert worker_body["evaluation_summary"]["execution_event"]["payload"]["tool_transcript"]
    assert worker_body["evaluation_summary"]["run_state"]["status"] == "completed"


def test_submit_with_details_high_risk_request_persists_incident(client, high_risk_payload):
    response = client.post("/v1/agent/submit-with-details", json=high_risk_payload)

    assert response.status_code == 200
    body = response.json()
    assert body["response"]["status"] in {"escalated", "denied"}

    incidents = client.get("/v1/incidents")
    assert incidents.status_code == 200
    assert len(incidents.json()) == 1
    assert incidents.json()[0]["run_id"] == body["response"]["run_id"]


def test_destructive_request_enters_approval_queue(client, low_risk_payload):
    payload = {
        **low_risk_payload,
        "task_type": "workflow",
        "allowed_tools": ["deploy_service"],
        "constraints": {
            **low_risk_payload["constraints"],
            "allow_destructive_actions": True,
            "requires_human_approval": False,
        },
    }

    response = client.post("/v1/agent/submit-with-details", json=payload)

    assert response.status_code == 200
    body = response.json()
    assert body["response"]["status"] == "awaiting_approval"
    assert body["execution_output"] == ""
    assert body["evaluation_summary"]["approval"]["status"] == "pending"

    approvals = client.get("/v1/approvals")
    assert approvals.status_code == 200
    assert len(approvals.json()) == 1
    assert approvals.json()[0]["run_id"] == body["response"]["run_id"]

    detail_page = client.get(f"/dashboard/runs/{body['response']['run_id']}")
    assert detail_page.status_code == 200
    assert "Approval" in detail_page.text
    assert "審核狀態" in detail_page.text

    decision = client.post(
        f"/v1/approvals/{body['response']['run_id']}/decision",
        json={"decision": "approve_once", "reviewer": "operator-1", "reason": "Allowed for demo."},
    )
    assert decision.status_code == 200
    assert decision.json()["status"] == "queued"


def test_open_tool_breaker_fails_closed(client, low_risk_payload):
    asyncio.run(
        client.state_store.open_breaker(
            "read_data",
            reason="operator_pause",
            ttl_seconds=300,
        )
    )

    response = client.post("/v1/agent/submit", json=low_risk_payload)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "awaiting_approval"
    assert "tool_circuit_breaker_open:read_data" in body["policy_flags"]


def test_dashboard_summary(client, low_risk_payload, high_risk_payload):
    client.post("/v1/agent/submit", json=low_risk_payload)
    client.post("/v1/agent/submit", json=high_risk_payload)

    response = client.get("/v1/dashboard/summary")

    assert response.status_code == 200
    body = response.json()
    assert body["total_runs"] == 2
    assert body["queued"] == 1
    assert body["escalated"] + body["denied"] == 1


def test_dashboard_pages_render(client):
    response = client.post(
        "/v1/agent/submit",
        json={
            "user_id": "demo-user",
            "task_type": "analysis",
            "goal": "Summarize the audit trail",
            "prompt": "Summarize the latest run status.",
            "allowed_tools": ["read_data"],
            "constraints": {
                "max_tool_calls": 3,
                "no_external_network": True,
                "requires_human_approval": False,
            },
        },
    )
    run_id = response.json()["run_id"]

    pages = [
        ("/dashboard", ["Guardrail Overview", "防護總覽", "Recent Runs", "近期執行", "Recent Incidents", "近期事件"]),
        ("/dashboard/runs", ["Runs", "執行紀錄"]),
        (f"/dashboard/runs/{run_id}", ["Run", "執行詳情", "Policy", "政策判定", "Evaluation / State", "評估與狀態"]),
        ("/dashboard/approvals", ["Approval Queue", "審核佇列"]),
        ("/dashboard/worker", ["Worker Operations", "Worker 作業狀態", "Dead Letter Queue", "死信佇列"]),
        ("/dashboard/observability", ["Observability Trends", "可觀測性趨勢", "SLO", "服務水準目標"]),
        ("/dashboard/playground", ["Playground", "測試沙盒"]),
    ]

    for path, expected_texts in pages:
        page = client.get(path)
        assert page.status_code == 200
        for expected in expected_texts:
            assert expected in page.text


def test_dashboard_playground_submit_queues_run(client):
    response = client.post(
        "/dashboard/playground",
        data={
            "user_id": "demo-user",
            "task_type": "code_fix",
            "allowed_tools": "read_repo, run_tests",
            "max_tool_calls": "8",
            "goal": "fix bug1699",
            "prompt": "修正會引起連鎖反應的 bug",
            "no_external_network": "on",
        },
    )

    assert response.status_code == 200
    assert "Result" in response.text
    assert "執行結果" in response.text
    assert "queued" in response.text
    assert "internal_server_error" not in response.text
