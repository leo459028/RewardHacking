from __future__ import annotations


def test_submit_persists_checkpoint_and_checkpoint_api(client, low_risk_payload):
    response = client.post(
        "/v1/agent/submit-with-details",
        headers={"X-Trace-Id": "trace-v14-checkpoint"},
        json=low_risk_payload,
    )
    assert response.status_code == 200
    run_id = response.json()["response"]["run_id"]

    checkpoint_response = client.get(f"/v1/runs/{run_id}/checkpoints")

    assert checkpoint_response.status_code == 200
    checkpoints = checkpoint_response.json()
    assert len(checkpoints) == 1
    checkpoint = checkpoints[0]
    assert checkpoint["run_id"] == run_id
    assert checkpoint["trace_id"] == "trace-v14-checkpoint"
    assert checkpoint["checkpoint_type"] == "submit"
    assert checkpoint["status"] == "queued"
    assert checkpoint["resume_cursor"] == "worker_queue"
    assert checkpoint["state_payload"]["execution_plan"]["workflow_id"] == "analysis-basic"
    assert checkpoint["state_payload"]["replay_ready"] is True


def test_resume_creates_resume_checkpoint_and_requeues_run(client, low_risk_payload):
    review_payload = {
        **low_risk_payload,
        "constraints": {
            **low_risk_payload["constraints"],
            "no_external_network": False,
        },
    }
    submit_response = client.post(
        "/v1/agent/submit-with-details",
        headers={"X-Trace-Id": "trace-v14-resume"},
        json=review_payload,
    )
    assert submit_response.status_code == 200
    run_id = submit_response.json()["response"]["run_id"]
    assert submit_response.json()["response"]["status"] == "awaiting_approval"

    resume_response = client.post(
        f"/v1/runs/{run_id}/resume",
        json={
            "resume_reason": "Manual reviewer approved a safe retry.",
            "resume_cursor": "worker_queue",
            "created_by": "qa-reviewer",
        },
    )

    assert resume_response.status_code == 200
    body = resume_response.json()
    assert body["run"]["run_id"] == run_id
    assert body["run"]["status"] == "queued"
    assert body["checkpoint"]["checkpoint_type"] == "resume"
    assert body["checkpoint"]["created_by"] == "qa-reviewer"
    assert body["checkpoint"]["state_payload"]["resume_reason"] == "Manual reviewer approved a safe retry."

    checkpoints = client.get(f"/v1/runs/{run_id}/checkpoints").json()
    assert [item["checkpoint_type"] for item in checkpoints] == ["submit", "resume"]


def test_replay_creates_safe_replay_attempt_without_external_execution(client, low_risk_payload):
    submit_response = client.post(
        "/v1/agent/submit-with-details",
        headers={"X-Trace-Id": "trace-v14-replay"},
        json=low_risk_payload,
    )
    assert submit_response.status_code == 200
    run_id = submit_response.json()["response"]["run_id"]

    replay_response = client.post(
        f"/v1/runs/{run_id}/replay",
        json={"mode": "dry_run", "created_by": "qa-replay"},
    )

    assert replay_response.status_code == 200
    replay = replay_response.json()
    assert replay["source_run_id"] == run_id
    assert replay["trace_id"] == "trace-v14-replay"
    assert replay["status"] == "completed"
    assert replay["mode"] == "dry_run"
    assert replay["result_payload"]["replayed_policy"]["decision"] == "allow"
    assert replay["result_payload"]["automatic_external_execution"] is False
    assert replay["result_payload"]["step_count"] == 3
    assert replay["limitations"]["external_tool_calls"] is False


def test_dashboard_surfaces_checkpoint_resume_replay_controls(client, low_risk_payload):
    response = client.post(
        "/v1/agent/submit-with-details",
        headers={"X-Trace-Id": "trace-v14-dashboard"},
        json=low_risk_payload,
    )
    run_id = response.json()["response"]["run_id"]

    detail = client.get(f"/dashboard/runs/{run_id}")
    overview = client.get("/dashboard/control-plane")

    assert detail.status_code == 200
    assert "Checkpoint / Resume" in detail.text
    assert f"/v1/runs/{run_id}/checkpoints" in detail.text
    assert f"/v1/runs/{run_id}/resume" in detail.text
    assert f"/v1/runs/{run_id}/replay" in detail.text
    assert "Replay Attempts" in detail.text
    assert overview.status_code == 200
    assert "Resumable" in overview.text
    assert "Replayable" in overview.text
