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
def operations_client(session_factory):
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


def reset_v08_settings(monkeypatch, **env):
    keys = [
        "APP_ENV",
        "AUTH_ENABLED",
        "API_KEYS",
        "QUOTA_ENABLED",
        "DASHBOARD_AUTH_ENABLED",
        "AUDIT_RETENTION_DAYS",
        "OPERATIONS_EVIDENCE_MAX_AGE_DAYS",
        "SECRET_ROTATION_MAX_AGE_DAYS",
        "RELEASE_GATE_MAX_QUEUED_AGE_MINUTES",
        "RELEASE_GATE_FAIL_ON_BOOTSTRAP_KEYS_IN_PROD",
    ]
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))
    get_settings.cache_clear()


def admin_headers(trace_id: str | None = None) -> dict[str, str]:
    headers = {"X-API-Key": "admin-key"}
    if trace_id:
        headers["X-Trace-Id"] = trace_id
    return headers


def test_admin_can_create_operations_evidence_and_readiness_uses_it(monkeypatch, session_factory):
    reset_v08_settings(
        monkeypatch,
        AUTH_ENABLED="true",
        API_KEYS="admin-key:default:admin,operator-key:default:operator",
        OPERATIONS_EVIDENCE_MAX_AGE_DAYS="30",
        SECRET_ROTATION_MAX_AGE_DAYS="90",
    )

    with operations_client(session_factory) as client:
        created = client.post(
            "/v1/operations/evidence",
            json={
                "evidence_type": "backup_restore_drill",
                "status": "pass",
                "summary": "Docker restore drill completed.",
                "reference_url": "docs/test_report.md#v08",
                "metadata": {"database": "postgres", "duration_minutes": 12},
            },
            headers=admin_headers("trace-v08-evidence"),
        )

        assert created.status_code == 200
        assert created.headers["x-trace-id"] == "trace-v08-evidence"
        evidence = created.json()
        assert evidence["evidence_type"] == "backup_restore_drill"
        assert evidence["status"] == "pass"
        assert evidence["created_by"] == "key:legacy"

        listed = client.get("/v1/operations/evidence", headers=admin_headers())
        assert listed.status_code == 200
        assert listed.json()[0]["summary"] == "Docker restore drill completed."

        readiness = client.get("/v1/operations/readiness", headers=admin_headers())
        assert readiness.status_code == 200
        body = readiness.json()
        assert body["status"] in {"pass", "warn", "fail"}
        backup_check = next(item for item in body["checks"] if item["check_key"] == "backup_restore_evidence")
        assert backup_check["status"] == "pass"


def test_release_gate_snapshot_persists_check_items(monkeypatch, session_factory):
    reset_v08_settings(
        monkeypatch,
        AUTH_ENABLED="true",
        API_KEYS="admin-key:default:admin",
        APP_ENV="prod",
        RELEASE_GATE_FAIL_ON_BOOTSTRAP_KEYS_IN_PROD="true",
    )

    with operations_client(session_factory) as client:
        created = client.post(
            "/v1/operations/release-gates",
            json={"release_version": "v0.8.0", "created_by": "release-manager"},
            headers=admin_headers("trace-v08-gate"),
        )

        assert created.status_code == 200
        gate = created.json()
        assert created.headers["x-trace-id"] == "trace-v08-gate"
        assert gate["release_version"] == "v0.8.0"
        assert gate["status"] == "fail"
        assert any(item["check_key"] == "bootstrap_keys_in_prod" for item in gate["items"])

        gates = client.get("/v1/operations/release-gates", headers=admin_headers())
        assert gates.status_code == 200
        assert gates.json()[0]["gate_id"] == gate["gate_id"]

        detail = client.get(f"/v1/operations/release-gates/{gate['gate_id']}", headers=admin_headers())
        assert detail.status_code == 200
        assert len(detail.json()["items"]) >= 3


def test_operations_api_requires_admin_for_mutations(monkeypatch, session_factory):
    reset_v08_settings(
        monkeypatch,
        AUTH_ENABLED="true",
        API_KEYS="admin-key:default:admin,operator-key:default:operator",
    )

    with operations_client(session_factory) as client:
        denied = client.post(
            "/v1/operations/evidence",
            json={
                "evidence_type": "secret_rotation",
                "status": "pass",
                "summary": "Rotated API keys.",
            },
            headers={"X-API-Key": "operator-key"},
        )

        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "insufficient_role"


def test_dashboard_operations_renders_and_can_create_evidence(monkeypatch, session_factory):
    reset_v08_settings(monkeypatch, AUTH_ENABLED="false", DASHBOARD_AUTH_ENABLED="false")

    with operations_client(session_factory) as client:
        page = client.get("/dashboard/operations")
        assert page.status_code == 200
        assert "Operations" in page.text
        assert "營運中心" in page.text
        assert "Release Gate" in page.text

        submitted = client.post(
            "/dashboard/operations/evidence",
            data={
                "evidence_type": "secret_rotation",
                "status": "pass",
                "summary": "Dashboard smoke secret rotation evidence.",
                "reference_url": "docs/test_report.md",
            },
        )
        assert submitted.status_code == 200
        assert "/dashboard/operations" in submitted.text

        page_after = client.get("/dashboard/operations")
        assert "Dashboard smoke secret rotation evidence." in page_after.text


def test_dashboard_favicon_does_not_create_console_noise(monkeypatch, session_factory):
    reset_v08_settings(monkeypatch, AUTH_ENABLED="false", DASHBOARD_AUTH_ENABLED="false")

    with operations_client(session_factory) as client:
        response = client.get("/favicon.ico")

        assert response.status_code == 204
        assert response.text == ""


def test_trace_id_header_is_returned_and_saved_on_submit(monkeypatch, session_factory, low_risk_payload):
    reset_v08_settings(monkeypatch, AUTH_ENABLED="false", QUOTA_ENABLED="false")

    with operations_client(session_factory) as client:
        response = client.post(
            "/v1/agent/submit",
            json=low_risk_payload,
            headers={"X-Trace-Id": "trace-v08-submit"},
        )

        assert response.status_code == 200
        assert response.headers["x-trace-id"] == "trace-v08-submit"
        run_id = response.json()["run_id"]
        run = client.get(f"/v1/runs/{run_id}")
        assert run.status_code == 200
        assert run.json()["request_payload"]["metadata"]["trace_id"] == "trace-v08-submit"
        assert client.event_publisher.events[0]["event"]["payload"]["request"]["metadata"]["trace_id"] == "trace-v08-submit"
