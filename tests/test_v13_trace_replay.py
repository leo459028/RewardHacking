from __future__ import annotations


def test_submit_details_persists_trace_steps_and_trace_api(client, low_risk_payload):
    response = client.post(
        "/v1/agent/submit-with-details",
        headers={"X-Trace-Id": "trace-v13-api"},
        json=low_risk_payload,
    )

    assert response.status_code == 200
    run_id = response.json()["response"]["run_id"]

    trace_response = client.get(f"/v1/runs/{run_id}/trace")

    assert trace_response.status_code == 200
    trace = trace_response.json()
    assert trace["trace_id"] == "trace-v13-api"
    assert trace["run"]["run_id"] == run_id
    assert trace["execution_plan"]["workflow_id"] == "analysis-basic"
    assert trace["execution_plan"]["trace_metadata"]["exporter_type"] == "internal"
    assert [step["step_type"] for step in trace["steps"]] == [
        "policy_check",
        "tool_execution",
        "evaluation",
    ]
    assert trace["steps"][0]["status"] == "completed"
    assert trace["steps"][0]["metadata"]["span_id"].startswith("span_")
    assert trace["steps"][0]["input_summary"]
    assert trace["steps"][0]["output_summary"]


def test_replay_bundle_contains_auditable_run_plan_and_tool_context(client, low_risk_payload):
    response = client.post(
        "/v1/agent/submit-with-details",
        headers={"X-Trace-Id": "trace-v13-replay"},
        json=low_risk_payload,
    )
    run_id = response.json()["response"]["run_id"]

    replay_response = client.get(f"/v1/runs/{run_id}/replay-bundle")

    assert replay_response.status_code == 200
    bundle = replay_response.json()
    assert bundle["schema_version"] == "control-plane-replay-bundle/v1.3"
    assert bundle["trace_context"]["trace_id"] == "trace-v13-replay"
    assert bundle["run"]["request_payload"]["metadata"]["trace_id"] == "trace-v13-replay"
    assert bundle["policy_decision"]["decision"] == "allow"
    assert bundle["execution_plan"]["run_id"] == run_id
    assert bundle["step_audit"][0]["run_id"] == run_id
    assert bundle["tool_references"][0]["name"] == "read_data"
    assert bundle["limitations"]["automatic_replay"] is False


def test_dashboard_surfaces_trace_replay_readiness(client, low_risk_payload):
    response = client.post(
        "/v1/agent/submit-with-details",
        headers={"X-Trace-Id": "trace-v13-dashboard"},
        json=low_risk_payload,
    )
    run_id = response.json()["response"]["run_id"]

    detail = client.get(f"/dashboard/runs/{run_id}")
    overview = client.get("/dashboard/control-plane")

    assert detail.status_code == 200
    assert "Trace / Replay" in detail.text
    assert "trace-v13-dashboard" in detail.text
    assert f"/v1/runs/{run_id}/replay-bundle" in detail.text
    assert "Step Audit" in detail.text
    assert overview.status_code == 200
    assert "Trace ID" in overview.text
    assert "Step Summary" in overview.text
    assert "Plan Health" in overview.text


def test_control_plane_step_metric_is_exported(client, low_risk_payload):
    response = client.post(
        "/v1/agent/submit-with-details",
        headers={"X-Trace-Id": "trace-v13-metrics"},
        json=low_risk_payload,
    )
    assert response.status_code == 200

    metrics = client.get("/v1/metrics")

    assert metrics.status_code == 200
    assert 'agent_control_plane_steps_total{status="completed",step_type="policy_check"}' in metrics.text
