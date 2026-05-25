from __future__ import annotations

from contextlib import contextmanager

from fastapi.testclient import TestClient

from app.api.deps import get_event_publisher, get_state_store
from app.core.config import get_settings
from app.infra.postgres import get_session
from app.main import app
from app.services.events import InMemoryEventPublisher
from app.services.run_state import InMemoryRunStateStore


@contextmanager
def governance_center_client(session_factory):
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


def reset_v07_settings(monkeypatch, **env):
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


def test_admin_can_create_tenant_and_dashboard_lists_it(monkeypatch, session_factory):
    reset_v07_settings(
        monkeypatch,
        AUTH_ENABLED="true",
        API_KEYS="admin-key:default:admin",
        QUOTA_ENABLED="true",
    )

    with governance_center_client(session_factory) as client:
        response = client.post(
            "/v1/tenants",
            json={
                "tenant_id": "tenant-v07",
                "display_name": "Tenant V07",
                "request_limit_per_minute": 2,
                "high_risk_limit_per_hour": 1,
            },
            headers={"X-API-Key": "admin-key"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["tenant_id"] == "tenant-v07"
        assert body["status"] == "active"
        assert body["request_limit_per_minute"] == 2
        assert body["high_risk_limit_per_hour"] == 1

        tenants = client.get("/v1/tenants", headers={"X-API-Key": "admin-key"})
        assert tenants.status_code == 200
        assert [tenant["tenant_id"] for tenant in tenants.json()] == ["tenant-v07"]

        dashboard = client.get("/dashboard/governance")
        assert dashboard.status_code == 200
        assert "Tenant V07" in dashboard.text
        assert "API Keys" in dashboard.text
        assert "API 金鑰" in dashboard.text


def test_db_managed_api_key_can_submit_until_revoked(monkeypatch, session_factory, low_risk_payload):
    reset_v07_settings(
        monkeypatch,
        AUTH_ENABLED="true",
        API_KEYS="admin-key:default:admin",
        QUOTA_ENABLED="false",
    )

    with governance_center_client(session_factory) as client:
        client.post(
            "/v1/tenants",
            json={"tenant_id": "tenant-api", "display_name": "Tenant API"},
            headers={"X-API-Key": "admin-key"},
        )
        key_response = client.post(
            "/v1/tenants/tenant-api/api-keys",
            json={"name": "operator key", "role": "operator"},
            headers={"X-API-Key": "admin-key"},
        )
        assert key_response.status_code == 200
        key_body = key_response.json()
        plaintext_key = key_body["plaintext_key"]
        assert plaintext_key.startswith("rg_")
        assert key_body["role"] == "operator"

        submit = client.post(
            "/v1/agent/submit",
            json={**low_risk_payload, "tenant_id": "tenant-api"},
            headers={"X-API-Key": plaintext_key},
        )
        assert submit.status_code == 200
        assert submit.json()["tenant_id"] == "tenant-api"

        revoke = client.post(
            f"/v1/tenants/tenant-api/api-keys/{key_body['key_id']}/revoke",
            headers={"X-API-Key": "admin-key"},
        )
        assert revoke.status_code == 200
        assert revoke.json()["status"] == "revoked"

        rejected = client.post(
            "/v1/agent/submit",
            json={**low_risk_payload, "tenant_id": "tenant-api"},
            headers={"X-API-Key": plaintext_key},
        )
        assert rejected.status_code == 401
        assert rejected.json()["error"]["code"] == "invalid_api_key"


def test_role_and_tenant_scope_are_enforced(monkeypatch, session_factory, low_risk_payload):
    reset_v07_settings(
        monkeypatch,
        AUTH_ENABLED="true",
        API_KEYS="admin-key:default:admin",
        QUOTA_ENABLED="false",
    )

    with governance_center_client(session_factory) as client:
        client.post(
            "/v1/tenants",
            json={"tenant_id": "tenant-view", "display_name": "Tenant View"},
            headers={"X-API-Key": "admin-key"},
        )
        viewer = client.post(
            "/v1/tenants/tenant-view/api-keys",
            json={"name": "viewer key", "role": "viewer"},
            headers={"X-API-Key": "admin-key"},
        ).json()["plaintext_key"]

        submit = client.post(
            "/v1/agent/submit",
            json={**low_risk_payload, "tenant_id": "tenant-view"},
            headers={"X-API-Key": viewer},
        )
        assert submit.status_code == 403
        assert submit.json()["error"]["code"] == "insufficient_role"

        mismatched = client.get(
            "/v1/runs",
            params={"tenant_id": "other-tenant"},
            headers={"X-API-Key": viewer},
        )
        assert mismatched.status_code == 403
        assert mismatched.json()["error"]["code"] == "tenant_mismatch"


def test_tenant_quota_settings_override_global_limits(monkeypatch, session_factory, low_risk_payload):
    reset_v07_settings(
        monkeypatch,
        AUTH_ENABLED="true",
        API_KEYS="admin-key:default:admin",
        QUOTA_ENABLED="true",
        TENANT_REQUEST_LIMIT_PER_MINUTE="99",
    )

    with governance_center_client(session_factory) as client:
        client.post(
            "/v1/tenants",
            json={
                "tenant_id": "tenant-quota",
                "display_name": "Tenant Quota",
                "request_limit_per_minute": 1,
                "high_risk_limit_per_hour": 5,
            },
            headers={"X-API-Key": "admin-key"},
        )
        key = client.post(
            "/v1/tenants/tenant-quota/api-keys",
            json={"name": "quota operator", "role": "operator"},
            headers={"X-API-Key": "admin-key"},
        ).json()["plaintext_key"]

        first = client.post(
            "/v1/agent/submit",
            json={**low_risk_payload, "tenant_id": "tenant-quota"},
            headers={"X-API-Key": key},
        )
        second = client.post(
            "/v1/agent/submit",
            json={**low_risk_payload, "tenant_id": "tenant-quota"},
            headers={"X-API-Key": key},
        )

        assert first.status_code == 200
        assert second.status_code == 429
        assert second.json()["error"]["code"] == "tenant_request_quota_exceeded"

        events = client.get(
            "/v1/governance/events",
            params={"tenant_id": "tenant-quota", "event_type": "request_quota_exceeded"},
            headers={"X-API-Key": key},
        )
        assert events.status_code == 200
        assert events.json()[0]["limit_value"] == 1
