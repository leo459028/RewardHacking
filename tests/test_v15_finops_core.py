from __future__ import annotations

import asyncio
from contextlib import contextmanager
from decimal import Decimal

from fastapi.testclient import TestClient

from app.api.deps import get_event_publisher, get_state_store
from app.core.config import get_settings
from app.infra.postgres import get_session
from app.main import app
from app.services.events import InMemoryEventPublisher
from app.services.run_state import InMemoryRunStateStore
from app.workers.consumer import process_run_by_id


@contextmanager
def finops_client(session_factory):
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


def reset_v15_settings(monkeypatch, **env):
    keys = [
        "AUTH_ENABLED",
        "API_KEYS",
        "QUOTA_ENABLED",
        "DASHBOARD_AUTH_ENABLED",
    ]
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))
    get_settings.cache_clear()


def test_pricing_catalog_crud_and_admin_rbac(monkeypatch, session_factory):
    reset_v15_settings(
        monkeypatch,
        AUTH_ENABLED="true",
        API_KEYS="admin-key:default:admin,viewer-key:default:viewer",
    )

    with finops_client(session_factory) as client:
        viewer_denied = client.post(
            "/v1/pricing",
            headers={"X-API-Key": "viewer-key"},
            json={
                "provider": "internal",
                "resource_type": "model",
                "model_name": "deterministic-control-plane",
                "price_unit": "per_call",
                "unit_price": 0.001,
                "currency": "USD",
            },
        )
        assert viewer_denied.status_code == 403

        created = client.post(
            "/v1/pricing",
            headers={"X-API-Key": "admin-key"},
            json={
                "provider": "internal",
                "resource_type": "model",
                "model_name": "deterministic-control-plane",
                "price_unit": "per_call",
                "unit_price": 0.001,
                "currency": "USD",
                "metadata": {"source": "test"},
            },
        )
        assert created.status_code == 200
        item = created.json()
        assert item["provider"] == "internal"
        assert item["is_active"] is True

        patched = client.patch(
            f"/v1/pricing/{item['pricing_id']}",
            headers={"X-API-Key": "admin-key"},
            json={"unit_price": 0.002, "metadata": {"source": "patched"}},
        )
        assert patched.status_code == 200
        assert Decimal(patched.json()["unit_price"]) == Decimal("0.002")

        disabled = client.delete(
            f"/v1/pricing/{item['pricing_id']}",
            headers={"X-API-Key": "admin-key"},
        )
        assert disabled.status_code == 200
        assert disabled.json()["is_active"] is False

        catalog = client.get("/v1/pricing", headers={"X-API-Key": "viewer-key"})
        assert catalog.status_code == 200
        assert catalog.json()[0]["pricing_id"] == item["pricing_id"]


def test_submit_records_estimated_usage_cost_and_dashboard_summary(monkeypatch, session_factory, low_risk_payload):
    reset_v15_settings(monkeypatch, AUTH_ENABLED="false", QUOTA_ENABLED="false")

    with finops_client(session_factory) as client:
        response = client.post(
            "/v1/agent/submit-with-details",
            headers={"X-Trace-Id": "trace-v15-cost"},
            json=low_risk_payload,
        )
        assert response.status_code == 200
        run_id = response.json()["response"]["run_id"]

        usage = client.get(f"/v1/usage-events/runs/{run_id}")
        cost = client.get(f"/v1/cost/runs/{run_id}")
        run_detail = client.get(f"/dashboard/runs/{run_id}")
        costs_page = client.get("/dashboard/costs")

        assert usage.status_code == 200
        assert {event["usage_unit"] for event in usage.json()} >= {"input_tokens", "tool_call"}
        assert cost.status_code == 200
        body = cost.json()
        assert body["run_id"] == run_id
        assert body["tenant_id"] == "default"
        assert Decimal(body["estimated_cost"]) > 0
        assert body["budget_status"] in {"within_budget", "warning", "exceeded"}
        assert run_detail.status_code == 200
        assert "Cost Summary" in run_detail.text
        assert costs_page.status_code == 200
        assert "Tenant Cost Summary" in costs_page.text


def test_worker_completion_records_actual_cost(monkeypatch, session_factory, low_risk_payload):
    reset_v15_settings(monkeypatch, AUTH_ENABLED="false", QUOTA_ENABLED="false")

    with finops_client(session_factory) as client:
        response = client.post(
            "/v1/agent/submit-with-details",
            headers={"X-Trace-Id": "trace-v15-worker"},
            json=low_risk_payload,
        )
        run_id = response.json()["response"]["run_id"]

        async def run_worker_once():
            async with session_factory() as session:
                return await process_run_by_id(
                    session,
                    run_id=run_id,
                    state_store=client.state_store,
                    event_publisher=client.event_publisher,
                    max_attempts=1,
                )

        asyncio.run(run_worker_once())

        cost = client.get(f"/v1/cost/runs/{run_id}").json()
        usage = client.get(f"/v1/usage-events/runs/{run_id}").json()
        assert Decimal(cost["actual_cost"]) > 0
        assert "actual" in {event["phase"] for event in usage}
        assert {"worker_seconds", "output_tokens"}.issubset({event["usage_unit"] for event in usage})


def test_budget_policy_records_violation_for_over_budget_run(monkeypatch, session_factory, low_risk_payload):
    reset_v15_settings(monkeypatch, AUTH_ENABLED="true", API_KEYS="admin-key:default:admin", QUOTA_ENABLED="false")

    with finops_client(session_factory) as client:
        budget = client.post(
            "/v1/budgets",
            headers={"X-API-Key": "admin-key"},
            json={
                "tenant_id": "default",
                "scope": "run",
                "policy_name": "Tiny run budget",
                "period": "per_run",
                "limit_amount": 0.000001,
                "currency": "USD",
                "actions": ["record_violation", "warn"],
            },
        )
        assert budget.status_code == 200

        submit = client.post(
            "/v1/agent/submit-with-details",
            headers={"X-API-Key": "admin-key", "X-Trace-Id": "trace-v15-budget"},
            json=low_risk_payload,
        )
        assert submit.status_code == 200
        run_id = submit.json()["response"]["run_id"]

        violations = client.get("/v1/budget-violations", headers={"X-API-Key": "admin-key"})
        assert violations.status_code == 200
        body = violations.json()
        assert body[0]["run_id"] == run_id
        assert body[0]["violation_type"] == "run_budget_exceeded"
        assert body[0]["actual_amount"] > body[0]["limit_amount"]


def test_replay_and_resume_create_linked_usage_events(monkeypatch, session_factory, low_risk_payload):
    reset_v15_settings(monkeypatch, AUTH_ENABLED="false", QUOTA_ENABLED="false")

    with finops_client(session_factory) as client:
        submit = client.post(
            "/v1/agent/submit-with-details",
            headers={"X-Trace-Id": "trace-v15-replay-resume"},
            json=low_risk_payload,
        )
        run_id = submit.json()["response"]["run_id"]

        replay = client.post(f"/v1/runs/{run_id}/replay", json={"mode": "dry_run", "created_by": "qa"})
        resume = client.post(
            f"/v1/runs/{run_id}/resume",
            json={"resume_reason": "Cost-linked retry", "resume_cursor": "worker_queue", "created_by": "qa"},
        )
        assert replay.status_code == 200
        assert resume.status_code == 200

        usage = client.get(f"/v1/usage-events/runs/{run_id}").json()
        replay_id = replay.json()["replay_id"]
        checkpoint_id = resume.json()["checkpoint"]["checkpoint_id"]
        assert any(event["replay_attempt_id"] == replay_id for event in usage)
        assert any(event["checkpoint_id"] == checkpoint_id for event in usage)
