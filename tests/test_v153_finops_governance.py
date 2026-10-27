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
from app.services.pricing_import import (
    PricingSeed,
    apply_pricing_seed,
    load_pricing_seed,
    validate_pricing_seed,
)
from app.services.run_state import InMemoryRunStateStore


@contextmanager
def v153_client(session_factory):
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


def reset_v153_settings(monkeypatch, **env):
    keys = [
        "APP_ENV",
        "AUTH_ENABLED",
        "API_KEYS",
        "QUOTA_ENABLED",
        "DASHBOARD_AUTH_ENABLED",
        "PRICING_SOURCE_STALE_DAYS",
        "FINOPS_BUDGET_ENFORCEMENT_ENABLED",
    ]
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))
    get_settings.cache_clear()


def test_v153_budget_hard_limit_fail_closed_blocks_run(
    monkeypatch,
    session_factory,
    low_risk_payload,
):
    reset_v153_settings(monkeypatch, AUTH_ENABLED="false", QUOTA_ENABLED="false")

    with v153_client(session_factory) as client:
        budget = client.post(
            "/v1/budgets",
            json={
                "name": "Tenant Hard Fail-Closed Budget",
                "tenant_id": "default",
                "scope_type": "tenant",
                "scope_id": "default",
                "period": "monthly",
                "hard_limit_amount": "0.000001",
                "currency": "USD",
                "actions_on_hard_limit": ["record_violation", "fail_closed"],
            },
        )
        assert budget.status_code == 200

        submit = client.post("/v1/agent/submit-with-details", json=low_risk_payload)
        assert submit.status_code == 200
        body = submit.json()
        run_id = body["response"]["run_id"]
        assert body["response"]["status"] == "denied"
        assert "finops_budget_hard_limit_exceeded" in body["response"]["policy_flags"]
        assert len(client.event_publisher.events) == 0

        violations = client.get(f"/v1/budget-violations?run_id={run_id}")
        assert violations.status_code == 200
        v_list = violations.json()
        assert len(v_list) == 1
        assert v_list[0]["violation_type"] in {"hard_limit_exceeded", "tenant_budget_hard_limit_exceeded"}
        assert v_list[0]["action_taken"] == "fail_closed"
        assert v_list[0]["decision"] in {"blocked", "fail_closed"}

        incidents = client.get(f"/v1/runs/{run_id}/incidents")
        assert incidents.status_code == 200
        assert any(item["code"] == "finops_budget_exceeded" for item in incidents.json())


def test_v153_budget_hard_limit_require_approval_creates_gate(
    monkeypatch,
    session_factory,
    low_risk_payload,
):
    reset_v153_settings(monkeypatch, AUTH_ENABLED="false", QUOTA_ENABLED="false")

    with v153_client(session_factory) as client:
        budget = client.post(
            "/v1/budgets",
            json={
                "name": "Workflow Approval Budget Gate",
                "tenant_id": "default",
                "scope_type": "workflow",
                "scope_id": "analysis-basic",
                "period": "per_run",
                "hard_limit_amount": "0.000001",
                "currency": "USD",
                "actions_on_hard_limit": ["record_violation", "require_approval"],
            },
        )
        assert budget.status_code == 200

        submit = client.post("/v1/agent/submit-with-details", json=low_risk_payload)
        assert submit.status_code == 200
        body = submit.json()
        run_id = body["response"]["run_id"]
        assert body["response"]["status"] == "awaiting_approval"
        assert "finops_budget_requires_approval" in body["response"]["policy_flags"]
        assert len(client.event_publisher.events) == 0

        plan = client.get(f"/v1/runs/{run_id}/execution-plan")
        assert plan.status_code == 200
        plan_json = plan.json()
        assert plan_json["status"] == "requires_approval"
        assert any(
            gate.get("gate_type") == "finops_budget_gate"
            for gate in plan_json["approval_gates"]
        )

        approvals = client.get("/v1/approvals")
        assert approvals.status_code == 200
        assert any(item["run_id"] == run_id for item in approvals.json())

        violations = client.get(f"/v1/budget-violations?run_id={run_id}")
        assert violations.status_code == 200
        v_list = violations.json()
        assert len(v_list) == 1
        assert v_list[0]["action_taken"] == "require_approval"
        assert v_list[0]["approval_id"] is not None


def test_v153_budget_soft_limit_degrade_model_updates_execution_plan(
    monkeypatch,
    session_factory,
    low_risk_payload,
):
    reset_v153_settings(monkeypatch, AUTH_ENABLED="false", QUOTA_ENABLED="false")

    with v153_client(session_factory) as client:
        budget = client.post(
            "/v1/budgets",
            json={
                "name": "Daily Soft Degrade Policy",
                "tenant_id": "default",
                "scope_type": "tenant",
                "scope_id": "default",
                "period": "daily",
                "soft_limit_amount": "0.000001",
                "hard_limit_amount": "10.000000",
                "currency": "USD",
                "fallback_model_tier": "economy",
                "actions_on_soft_limit": ["warn", "record_violation", "degrade_model"],
                "actions_on_hard_limit": ["record_violation", "fail_closed"],
            },
        )
        assert budget.status_code == 200

        submit = client.post("/v1/agent/submit-with-details", json=low_risk_payload)
        assert submit.status_code == 200
        body = submit.json()
        run_id = body["response"]["run_id"]
        assert body["response"]["status"] == "queued"
        assert "finops_budget_degraded_model" in body["response"]["policy_flags"]

        plan = client.get(f"/v1/runs/{run_id}/execution-plan")
        assert plan.status_code == 200
        route = plan.json()["model_route"]
        assert route["fallback_model_tier"] == "economy"
        assert route["model"] == "deterministic-economy"

        violations = client.get(f"/v1/budget-violations?run_id={run_id}")
        assert violations.status_code == 200
        v_list = violations.json()
        assert len(v_list) == 1
        assert v_list[0]["violation_type"] in {"soft_limit_exceeded", "tenant_budget_soft_limit_exceeded"}
        assert v_list[0]["action_taken"] == "degrade_model"
        assert v_list[0]["decision"] in {"degraded", "degrade_model"}


def test_v153_pricing_lookup_context_tier_scope_priority_and_effective_window(
    monkeypatch,
    session_factory,
):
    reset_v153_settings(monkeypatch, AUTH_ENABLED="false", QUOTA_ENABLED="false")

    with v153_client(session_factory) as client:
        imported = client.post(
            "/v1/admin/finops/pricing/import/apply",
            json={"file_path": "app/seeds/model_pricing_seed.yaml", "created_by": "pytest"},
        )
        assert imported.status_code == 200

        # 1. Context-tiered lookup for gemini-3.1-pro-preview: <=200k ($2.00/1M) vs >200k ($4.00/1M)
        u_low = client.post(
            "/v1/usage-events",
            json={
                "run_id": "run-tier-low",
                "tenant_id": "default",
                "provider": "google",
                "resource_type": "model",
                "model_name": "gemini-3.1-pro-preview",
                "token_type": "input",
                "pricing_mode": "standard",
                "input_tokens": 100000,
                "quantity": "100000",
                "unit": "tokens",
            },
        )
        assert u_low.status_code == 200
        cost_low = client.get("/v1/cost/runs/run-tier-low").json()
        assert Decimal(cost_low["cost_events"][0]["unit_price_snapshot"]) == Decimal("2.00")
        assert Decimal(cost_low["actual_cost"]) == Decimal("0.20000000")

        u_high = client.post(
            "/v1/usage-events",
            json={
                "run_id": "run-tier-high",
                "tenant_id": "default",
                "provider": "google",
                "resource_type": "model",
                "model_name": "gemini-3.1-pro-preview",
                "token_type": "input",
                "pricing_mode": "standard",
                "input_tokens": 250000,
                "quantity": "250000",
                "unit": "tokens",
            },
        )
        assert u_high.status_code == 200
        cost_high = client.get("/v1/cost/runs/run-tier-high").json()
        assert Decimal(cost_high["cost_events"][0]["unit_price_snapshot"]) == Decimal("4.00")
        assert Decimal(cost_high["actual_cost"]) == Decimal("1.00000000")

        # 2. Scope precedence: tenant_override for tenant-vip overrides system_default
        override_row = client.post(
            "/v1/pricing",
            json={
                "provider": "google",
                "resource_type": "model",
                "model_name": "gemini-3.1-pro-preview",
                "pricing_mode": "standard",
                "token_type": "input",
                "price_unit": "per_1m_tokens",
                "unit_price": "1.25",
                "currency": "USD",
                "scope_type": "tenant_override",
                "scope_id": "tenant-vip",
            },
        )
        assert override_row.status_code == 200

        # 3. Expired row should not match
        expired_row = client.post(
            "/v1/pricing",
            json={
                "provider": "google",
                "resource_type": "model",
                "model_name": "gemini-3.1-pro-preview",
                "pricing_mode": "standard",
                "token_type": "input",
                "price_unit": "per_1m_tokens",
                "unit_price": "0.10",
                "currency": "USD",
                "scope_type": "tenant_override",
                "scope_id": "tenant-vip",
                "effective_from": "2024-01-01T00:00:00Z",
                "effective_to": "2024-06-01T00:00:00Z",
            },
        )
        assert expired_row.status_code == 200

        u_vip = client.post(
            "/v1/usage-events",
            json={
                "run_id": "run-tier-vip",
                "tenant_id": "tenant-vip",
                "provider": "google",
                "resource_type": "model",
                "model_name": "gemini-3.1-pro-preview",
                "token_type": "input",
                "pricing_mode": "standard",
                "input_tokens": 100000,
                "quantity": "100000",
                "unit": "tokens",
            },
        )
        assert u_vip.status_code == 200
        cost_vip = client.get("/v1/cost/runs/run-tier-vip").json()
        assert Decimal(cost_vip["cost_events"][0]["unit_price_snapshot"]) == Decimal("1.25")


def test_v153_pricing_free_quota_minimum_billable_and_savings_summary(
    monkeypatch,
    session_factory,
):
    reset_v153_settings(monkeypatch, AUTH_ENABLED="false", QUOTA_ENABLED="false")

    with v153_client(session_factory) as client:
        imported = client.post(
            "/v1/admin/finops/pricing/import/apply",
            json={"file_path": "app/seeds/model_pricing_seed.yaml", "created_by": "pytest"},
        )
        assert imported.status_code == 200

        custom_pricing = client.post(
            "/v1/pricing",
            json={
                "provider": "custom",
                "resource_type": "tool",
                "resource_subtype": "web_search",
                "model_name": "vector-v1",
                "pricing_mode": "standard",
                "token_type": "not_applicable",
                "price_unit": "per_call",
                "unit_price": "0.01",
                "currency": "USD",
                "free_quota_quantity": "5",
                "free_quota_period": "monthly",
                "minimum_billable_quantity": "10",
                "scope_type": "system_default",
                "scope_id": "default",
            },
        )
        assert custom_pricing.status_code == 200

        # Usage of 8 calls: 5 free quota deducted -> 3 remaining -> bumped to minimum_billable_quantity 10 -> cost = 10 * 0.01 = 0.10
        u_quota = client.post(
            "/v1/usage-events",
            json={
                "run_id": "run-quota-test",
                "tenant_id": "default",
                "provider": "custom",
                "resource_type": "tool",
                "resource_subtype": "web_search",
                "model_name": "vector-v1",
                "token_type": "not_applicable",
                "pricing_mode": "standard",
                "quantity": "8",
                "usage_unit": "per_call",
            },
        )
        assert u_quota.status_code == 200
        run_quota_cost = client.get("/v1/cost/runs/run-quota-test").json()
        assert Decimal(run_quota_cost["actual_cost"]) == Decimal("0.10000000")
        meta = run_quota_cost["cost_events"][0]["metadata"]
        assert Decimal(meta["free_quota_applied_quantity"]) == Decimal("5")
        assert Decimal(meta["billable_quantity"]) == Decimal("10")

        # Record cached_input and batch usage on openai/gpt-5.4 (standard input=$2.50/1M, cached_input=$0.25/1M, batch input=$1.25/1M)
        u_cache = client.post(
            "/v1/usage-events",
            json={
                "run_id": "run-savings-test",
                "tenant_id": "default",
                "provider": "openai",
                "resource_type": "model",
                "model_name": "gpt-5.4",
                "token_type": "cached_input",
                "pricing_mode": "cache",
                "cached_input_tokens": 100000,
                "quantity": "100000",
                "unit": "tokens",
            },
        )
        assert u_cache.status_code == 200

        u_batch = client.post(
            "/v1/usage-events",
            json={
                "run_id": "run-savings-test",
                "tenant_id": "default",
                "provider": "openai",
                "resource_type": "model",
                "model_name": "gpt-5.4",
                "token_type": "input",
                "pricing_mode": "batch",
                "input_tokens": 100000,
                "quantity": "100000",
                "unit": "tokens",
            },
        )
        assert u_batch.status_code == 200

        run_savings = client.get("/v1/cost/runs/run-savings-test").json()
        assert Decimal(run_savings["cache_savings"]) > Decimal("0")
        assert Decimal(run_savings["batch_savings"]) > Decimal("0")

        tenant_savings = client.get("/v1/cost/tenants/default").json()
        assert Decimal(tenant_savings["cache_savings"]) >= Decimal(run_savings["cache_savings"])
        assert Decimal(tenant_savings["batch_savings"]) >= Decimal(run_savings["batch_savings"])


def test_v153_budget_and_violation_lifecycle_endpoints_and_ui(
    monkeypatch,
    session_factory,
    low_risk_payload,
):
    reset_v153_settings(
        monkeypatch,
        AUTH_ENABLED="true",
        API_KEYS="admin-key:default:admin,operator-key:default:operator,viewer-key:default:viewer",
        QUOTA_ENABLED="false",
        DASHBOARD_AUTH_ENABLED="false",
    )

    with v153_client(session_factory) as client:
        # Create budget via UI POST
        ui_create = client.post(
            "/dashboard/admin/finops/budgets",
            data={
                "name": "UI Per Run Budget",
                "tenant_id": "default",
                "scope_type": "run",
                "scope_id": "default",
                "period": "per_run",
                "soft_limit_amount": "0.000001",
                "hard_limit_amount": "0.000002",
                "fallback_model_tier": "economy",
                "actions_on_soft_limit": "warn,record_violation",
                "actions_on_hard_limit": "record_violation,fail_closed",
                "currency": "USD",
            },
            follow_redirects=False,
        )
        assert ui_create.status_code == 303

        budgets_page = client.get("/dashboard/admin/finops/budgets")
        assert budgets_page.status_code == 200
        assert "UI Per Run Budget" in budgets_page.text

        budgets = client.get("/v1/budgets", headers={"X-API-Key": "viewer-key"}).json()
        assert len(budgets) == 1
        budget_id = budgets[0]["budget_id"]

        single_budget = client.get(f"/v1/budgets/{budget_id}", headers={"X-API-Key": "viewer-key"})
        assert single_budget.status_code == 200
        assert single_budget.json()["budget_id"] == budget_id

        # Trigger violation
        submit = client.post(
            "/v1/agent/submit-with-details",
            headers={"X-API-Key": "operator-key"},
            json=low_risk_payload,
        )
        assert submit.status_code == 200

        v_list = client.get("/v1/budget-violations", headers={"X-API-Key": "viewer-key"}).json()
        assert len(v_list) >= 1
        violation_id = v_list[0]["violation_id"]

        single_v = client.get(
            f"/v1/budget-violations/{violation_id}",
            headers={"X-API-Key": "viewer-key"},
        )
        assert single_v.status_code == 200

        # Viewer cannot acknowledge or resolve
        viewer_ack = client.post(
            f"/v1/budget-violations/{violation_id}/acknowledge",
            headers={"X-API-Key": "viewer-key"},
            json={"notes": "viewer attempt"},
        )
        assert viewer_ack.status_code == 403

        # Operator can acknowledge
        op_ack = client.post(
            f"/v1/budget-violations/{violation_id}/acknowledge",
            headers={"X-API-Key": "operator-key"},
            json={"notes": "investigating cost spike"},
        )
        assert op_ack.status_code == 200
        assert op_ack.json()["resolution_status"] == "acknowledged"

        # Operator cannot resolve (requires finops_admin/super_admin/admin)
        op_res = client.post(
            f"/v1/budget-violations/{violation_id}/resolve",
            headers={"X-API-Key": "operator-key"},
            json={"resolution_status": "resolved", "notes": "operator resolve"},
        )
        assert op_res.status_code == 403

        # Admin can resolve
        adm_res = client.post(
            f"/v1/budget-violations/{violation_id}/resolve",
            headers={"X-API-Key": "admin-key"},
            json={"resolution_status": "resolved", "notes": "budget adjusted"},
        )
        assert adm_res.status_code == 200
        assert adm_res.json()["resolution_status"] == "resolved"

        filtered = client.get(
            "/v1/budget-violations?resolution_status=resolved",
            headers={"X-API-Key": "viewer-key"},
        )
        assert filtered.status_code == 200
        assert len(filtered.json()) == 1

        # Disable budget via API
        disabled = client.post(
            f"/v1/budgets/{budget_id}/disable",
            headers={"X-API-Key": "admin-key"},
        )
        assert disabled.status_code == 200
        assert disabled.json()["is_active"] is False


def test_v153_manual_pricing_edit_audit_log_and_no_scientific_notation(
    monkeypatch,
    session_factory,
):
    reset_v153_settings(monkeypatch, AUTH_ENABLED="false", DASHBOARD_AUTH_ENABLED="false")

    with v153_client(session_factory) as client:
        client.post(
            "/v1/admin/finops/pricing/import/apply",
            json={"file_path": "app/seeds/model_pricing_seed.yaml", "created_by": "pytest"},
        )
        catalog = client.get("/v1/pricing").json()
        pricing_id = catalog[0]["pricing_id"]

        patched = client.patch(
            f"/v1/admin/finops/pricing/{pricing_id}",
            json={
                "unit_price": "0.0025",
                "change_reason": "Manual rate adjustment",
                "supersede_existing": False,
            },
        )
        assert patched.status_code == 200

        superseded = client.patch(
            f"/v1/admin/finops/pricing/{pricing_id}",
            json={
                "unit_price": "0.0035",
                "change_reason": "New contract version",
                "supersede_existing": True,
            },
        )
        assert superseded.status_code == 200
        new_pricing_id = superseded.json()["pricing_id"]
        assert new_pricing_id != pricing_id
        assert superseded.json()["supersedes_pricing_id"] == pricing_id

        disabled = client.post(f"/v1/admin/finops/pricing/{new_pricing_id}/disable")
        assert disabled.status_code == 200

        changes = client.get("/v1/admin/finops/pricing/changes").json()
        change_types = [item["change_type"] for item in changes]
        assert "update" in change_types
        assert "supersede" in change_types
        assert "disable" in change_types

        for url in [
            "/dashboard/costs",
            "/dashboard/admin/finops/pricing",
            f"/dashboard/admin/finops/pricing/{pricing_id}",
        ]:
            page = client.get(url)
            assert page.status_code == 200
            assert "0E-8" not in page.text
            assert "0e-8" not in page.text


def test_v153_pricing_seed_stale_source_and_strict_mode(
    monkeypatch,
    session_factory,
):
    reset_v153_settings(monkeypatch, PRICING_SOURCE_STALE_DAYS="30")
    seed = load_pricing_seed("app/seeds/model_pricing_seed.yaml")
    stale_seed = PricingSeed(
        schema_version=seed.schema_version,
        checked_at="2020-01-01T00:00:00Z",
        base_currency=seed.base_currency,
        import_policy=dict(seed.import_policy),
        source_documents=[
            {
                **seed.source_documents[0],
                "checked_at": "2020-01-01T00:00:00Z",
            }
        ],
        pricing_items=list(seed.pricing_items),
        source_file_name=seed.source_file_name,
        source_file_hash=seed.source_file_hash,
    )

    report = validate_pricing_seed(stale_seed, strict=True, allow_warnings=False)
    assert len(report.stale_sources) >= 1
    assert len(report.warnings) >= 1
    assert report.status == "invalid"

    async def _run_strict_apply():
        async with session_factory() as session:
            applied_report = await apply_pricing_seed(
                session,
                stale_seed,
                created_by="pytest",
                strict=True,
                allow_warnings=False,
            )
            assert applied_report.status == "invalid"
            assert applied_report.new_rows >= 0

    asyncio.run(_run_strict_apply())


def test_v153_finops_metrics_observability_and_operations_readiness(
    monkeypatch,
    session_factory,
    low_risk_payload,
):
    reset_v153_settings(
        monkeypatch,
        APP_ENV="prod",
        AUTH_ENABLED="false",
        QUOTA_ENABLED="false",
    )

    with v153_client(session_factory) as client:
        client.post(
            "/v1/admin/finops/pricing/import/dry-run",
            json={"file_path": "app/seeds/model_pricing_seed.yaml"},
        )
        client.post(
            "/v1/budgets",
            json={
                "name": "Prod Critical Gate",
                "tenant_id": "default",
                "scope_type": "tenant",
                "scope_id": "default",
                "period": "monthly",
                "hard_limit_amount": "0.000001",
                "currency": "USD",
                "actions_on_hard_limit": ["record_violation", "fail_closed"],
            },
        )
        client.post("/v1/agent/submit-with-details", json=low_risk_payload)

        metrics_text = client.get("/v1/metrics").text
        assert "agent_finops_usage_events_total" in metrics_text
        assert "agent_finops_cost_usd_total" in metrics_text
        assert "agent_finops_budget_violations_total" in metrics_text
        assert "agent_finops_pricing_imports_total" in metrics_text

        obs = client.get("/v1/observability/summary").json()
        assert "finops" in obs
        assert obs["finops"]["cost_events"] >= 1
        assert obs["finops"]["open_violations"] >= 1
        assert obs["finops"]["budget_blocked_runs"] >= 1

        readiness = client.get("/v1/operations/readiness").json()
        checks = {item["check_key"]: item for item in readiness["checks"]}
        assert "finops_pricing_catalog_ready" in checks
        assert checks["finops_pricing_catalog_ready"]["status"] == "pass"
        assert "finops_open_critical_violations" in checks
        assert checks["finops_open_critical_violations"]["status"] == "fail"
