from __future__ import annotations

import asyncio
import base64
from contextlib import contextmanager

from fastapi.testclient import TestClient

from app.api.deps import get_event_publisher, get_state_store
from app.core.config import get_settings
from app.infra.postgres import get_session
from app.main import app
from app.services.events import InMemoryEventPublisher
from app.services.run_state import InMemoryRunStateStore


@contextmanager
def governance_client(session_factory):
    state_store = InMemoryRunStateStore()
    event_publisher = InMemoryEventPublisher()

    async def override_get_session():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_state_store] = lambda: state_store
    app.dependency_overrides[get_event_publisher] = lambda: event_publisher
    with TestClient(app) as client:
        client.state_store = state_store
        client.event_publisher = event_publisher
        yield client
    app.dependency_overrides.clear()


def reset_settings(monkeypatch, **env):
    keys = [
        "AUTH_ENABLED",
        "API_KEYS",
        "QUOTA_ENABLED",
        "TENANT_REQUEST_LIMIT_PER_MINUTE",
        "TENANT_HIGH_RISK_LIMIT_PER_HOUR",
        "DASHBOARD_AUTH_ENABLED",
    ]
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))
    get_settings.cache_clear()


def test_tenant_id_is_persisted_and_filters_runs(monkeypatch, session_factory, low_risk_payload):
    reset_settings(monkeypatch, QUOTA_ENABLED="false")

    with governance_client(session_factory) as client:
        tenant_a_payload = {**low_risk_payload, "tenant_id": "tenant-a"}
        tenant_b_payload = {**low_risk_payload, "tenant_id": "tenant-b", "user_id": "u2"}

        tenant_a = client.post("/v1/agent/submit", json=tenant_a_payload)
        tenant_b = client.post("/v1/agent/submit", json=tenant_b_payload)

        assert tenant_a.status_code == 200
        assert tenant_b.status_code == 200

        filtered = client.get("/v1/runs", params={"tenant_id": "tenant-a"})
        assert filtered.status_code == 200
        runs = filtered.json()
        assert len(runs) == 1
        assert runs[0]["tenant_id"] == "tenant-a"
        assert runs[0]["run_id"] == tenant_a.json()["run_id"]


def test_api_key_auth_assigns_tenant_and_rejects_invalid_access(monkeypatch, session_factory, low_risk_payload):
    reset_settings(
        monkeypatch,
        AUTH_ENABLED="true",
        API_KEYS="key-a:tenant-a",
        QUOTA_ENABLED="false",
    )

    with governance_client(session_factory) as client:
        missing = client.post("/v1/agent/submit", json=low_risk_payload)
        invalid = client.post(
            "/v1/agent/submit",
            json=low_risk_payload,
            headers={"X-API-Key": "wrong"},
        )
        mismatch = client.post(
            "/v1/agent/submit",
            json={**low_risk_payload, "tenant_id": "tenant-b"},
            headers={"X-API-Key": "key-a"},
        )
        accepted = client.post(
            "/v1/agent/submit",
            json=low_risk_payload,
            headers={"X-API-Key": "key-a"},
        )

        assert missing.status_code == 401
        assert missing.json()["error"]["code"] == "auth_required"
        assert invalid.status_code == 401
        assert invalid.json()["error"]["code"] == "invalid_api_key"
        assert mismatch.status_code == 403
        assert mismatch.json()["error"]["code"] == "tenant_mismatch"
        assert accepted.status_code == 200

        detail = client.get(
            f"/v1/runs/{accepted.json()['run_id']}",
            headers={"X-API-Key": "key-a"},
        )
        assert detail.status_code == 200
        assert detail.json()["tenant_id"] == "tenant-a"


def test_request_quota_returns_429_and_records_governance_event(
    monkeypatch,
    session_factory,
    low_risk_payload,
):
    reset_settings(
        monkeypatch,
        QUOTA_ENABLED="true",
        TENANT_REQUEST_LIMIT_PER_MINUTE="1",
        TENANT_HIGH_RISK_LIMIT_PER_HOUR="99",
    )

    with governance_client(session_factory) as client:
        payload = {**low_risk_payload, "tenant_id": "tenant-a"}
        first = client.post("/v1/agent/submit", json=payload)
        second = client.post("/v1/agent/submit", json=payload)

        assert first.status_code == 200
        assert second.status_code == 429
        assert second.json()["error"]["code"] == "tenant_request_quota_exceeded"

        events = client.get("/v1/governance/events", params={"tenant_id": "tenant-a"})
        assert events.status_code == 200
        assert events.json()[0]["event_type"] == "request_quota_exceeded"
        assert events.json()[0]["tenant_id"] == "tenant-a"
        assert events.json()[0]["decision"] == "blocked"


def test_high_risk_quota_fail_closed_marks_run_for_approval(
    monkeypatch,
    session_factory,
    high_risk_payload,
):
    reset_settings(
        monkeypatch,
        QUOTA_ENABLED="true",
        TENANT_REQUEST_LIMIT_PER_MINUTE="99",
        TENANT_HIGH_RISK_LIMIT_PER_HOUR="0",
    )

    with governance_client(session_factory) as client:
        response = client.post(
            "/v1/agent/submit-with-details",
            json={
                **high_risk_payload,
                "tenant_id": "tenant-risk",
                "prompt": "Review this workflow request and summarize the risks.",
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["response"]["status"] == "awaiting_approval"
        assert "tenant_high_risk_quota_exceeded" in body["response"]["policy_flags"]
        assert client.event_publisher.events == []

        events = client.get("/v1/governance/events", params={"tenant_id": "tenant-risk"})
        assert events.status_code == 200
        assert events.json()[0]["event_type"] == "high_risk_quota_exceeded"


def test_governance_summary_and_dashboard_render(monkeypatch, session_factory, low_risk_payload):
    reset_settings(monkeypatch, QUOTA_ENABLED="false")

    with governance_client(session_factory) as client:
        response = client.post(
            "/v1/agent/submit",
            json={**low_risk_payload, "tenant_id": "tenant-a"},
        )
        assert response.status_code == 200

        summary = client.get("/v1/governance/summary", params={"tenant_id": "tenant-a"})
        assert summary.status_code == 200
        assert summary.json()["tenant_id"] == "tenant-a"
        assert summary.json()["total_runs"] == 1
        assert summary.json()["quota_limited"] == 0

        dashboard = client.get("/dashboard/governance")
        assert dashboard.status_code == 200
        assert "Governance" in dashboard.text
        assert "治理中心" in dashboard.text
        assert "tenant-a" in dashboard.text

        runs = client.get("/dashboard/runs", params={"tenant_id": "tenant-a"})
        assert runs.status_code == 200
        assert "tenant-a" in runs.text


def test_dashboard_basic_auth_can_protect_dashboard(monkeypatch, session_factory):
    reset_settings(
        monkeypatch,
        QUOTA_ENABLED="false",
        DASHBOARD_AUTH_ENABLED="true",
        DASHBOARD_USERNAME="operator",
        DASHBOARD_PASSWORD="secret",
    )

    with governance_client(session_factory) as client:
        missing = client.get("/dashboard")
        token = base64.b64encode(b"operator:secret").decode("ascii")
        accepted = client.get("/dashboard", headers={"Authorization": f"Basic {token}"})

        assert missing.status_code == 401
        assert missing.headers["www-authenticate"] == "Basic"
        assert accepted.status_code == 200
        assert "Guardrail Overview" in accepted.text


def test_quota_counter_uses_in_memory_store_directly(monkeypatch):
    reset_settings(
        monkeypatch,
        QUOTA_ENABLED="true",
        TENANT_REQUEST_LIMIT_PER_MINUTE="1",
    )
    store = InMemoryRunStateStore()

    async def exercise():
        first = await store.increment_quota("tenant-a", "requests", limit=1, ttl_seconds=60)
        second = await store.increment_quota("tenant-a", "requests", limit=1, ttl_seconds=60)
        return first, second

    first, second = asyncio.run(exercise())
    assert first.allowed is True
    assert first.remaining == 0
    assert second.allowed is False
    assert second.used == 2
