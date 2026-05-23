def test_healthz_and_readyz(client):
    health = client.get("/v1/healthz")
    ready = client.get("/v1/readyz")

    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready"}


def test_submit_low_risk_request_persists_run(client, low_risk_payload):
    response = client.post("/v1/agent/submit", json=low_risk_payload)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"

    runs = client.get("/v1/runs")
    assert runs.status_code == 200
    assert len(runs.json()) == 1
    assert runs.json()[0]["run_id"] == body["run_id"]

    detail = client.get(f"/v1/runs/{body['run_id']}")
    assert detail.status_code == 200
    assert detail.json()["execution_output"]


def test_submit_with_details_high_risk_request_persists_incident(client, high_risk_payload):
    response = client.post("/v1/agent/submit-with-details", json=high_risk_payload)

    assert response.status_code == 200
    body = response.json()
    assert body["response"]["status"] in {"escalated", "denied"}

    incidents = client.get("/v1/incidents")
    assert incidents.status_code == 200
    assert len(incidents.json()) == 1
    assert incidents.json()[0]["run_id"] == body["response"]["run_id"]


def test_dashboard_summary(client, low_risk_payload, high_risk_payload):
    client.post("/v1/agent/submit", json=low_risk_payload)
    client.post("/v1/agent/submit", json=high_risk_payload)

    response = client.get("/v1/dashboard/summary")

    assert response.status_code == 200
    body = response.json()
    assert body["total_runs"] == 2
    assert body["completed"] == 1
    assert body["escalated"] + body["denied"] == 1


def test_dashboard_pages_render(client):
    dashboard = client.get("/dashboard")
    playground = client.get("/dashboard/playground")

    assert dashboard.status_code == 200
    assert "Guardrail Overview" in dashboard.text
    assert playground.status_code == 200
    assert "Playground" in playground.text
