from __future__ import annotations

import asyncio
import json
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.models.agent_run import AgentRun
from app.models.control_plane import ExecutionPlan, ToolDefinition
from app.models.finops import CostEvent, UsageEvent
from app.models.governance_event import GovernanceEvent
from app.models.incident import Incident
from app.models.tool_execution import ToolExecution
from app.schemas.agent import AgentRequest
from app.services.finops import CSV_EXPORT_COLUMNS, FINOPS_CALCULATION_VERSION
from app.services.mcp_gateway import (
    McpGatewayContext,
    is_external_endpoint,
    sanitize_tool_arguments,
    validate_tool_arguments,
)
from app.services.policy_engine import PolicyEngine
from app.services.run_state import InMemoryRunStateStore
from app.workers.consumer import process_run_by_id
from tests.test_v15_finops_core import finops_client, reset_v15_settings

FAKE_API_KEY = "sk-secret1234567890"


def _payload(task_type: str, tools: list[str], *, tenant: str = "v157-tenant", args: dict | None = None, **constraints) -> dict:
    base_constraints = {
        "no_external_network": True,
        "requires_human_approval": False,
        "max_tool_calls": 4,
        "allow_destructive_actions": False,
        "allow_pii_access": False,
    }
    base_constraints.update(constraints)
    body = {
        "tenant_id": tenant,
        "user_id": "v157-user",
        "task_type": task_type,
        "prompt": "Analyze the operational dataset carefully.",
        "goal": "Summarize findings",
        "allowed_tools": tools,
        "constraints": base_constraints,
    }
    if args is not None:
        body["tool_arguments"] = args
    return body


def _run_worker(session_factory, run_id: str, client) -> None:
    async def _do():
        async with session_factory() as session:
            await process_run_by_id(
                session,
                run_id=run_id,
                state_store=client.state_store,
                event_publisher=client.event_publisher,
            )

    asyncio.run(_do())


def _query(session_factory, stmt):
    async def _do():
        async with session_factory() as session:
            return list((await session.scalars(stmt)).all())

    return asyncio.run(_do())


def _register_server(client, server_id: str, **overrides) -> dict:
    body = {
        "mcp_server_id": server_id,
        "display_name": server_id,
        "transport": "stdio",
        "endpoint": "local://stdio",
        "trust_tier": "verified",
        "quota_per_minute": 60,
    }
    body.update(overrides)
    res = client.post("/v1/control-plane/mcp-servers", json=body)
    assert res.status_code == 200, res.text
    return res.json()


def _register_tool(client, tool_name: str, server_id: str, *, task_types=None, metadata=None) -> None:
    meta = {"mcp_server_id": server_id}
    meta.update(metadata or {})
    res = client.post(
        "/v1/control-plane/tools",
        json={
            "tool_name": tool_name,
            "display_name": tool_name,
            "task_types": task_types or ["analysis"],
            "risk_level": "medium",
            "metadata": meta,
        },
    )
    assert res.status_code == 200, res.text


def test_v157_mcp_server_registry_crud_and_breaker_controls(monkeypatch, session_factory):
    reset_v15_settings(
        monkeypatch,
        AUTH_ENABLED="true",
        API_KEYS="admin-key:default:admin,viewer-key:default:viewer",
        QUOTA_ENABLED="false",
    )
    admin = {"X-API-Key": "admin-key"}
    viewer = {"X-API-Key": "viewer-key"}
    with finops_client(session_factory) as client:
        listed = client.get("/v1/control-plane/mcp-servers", headers=viewer)
        assert listed.status_code == 200
        ids = {item["mcp_server_id"] for item in listed.json()}
        assert {"mcp-core-local", "mcp-data-verified", "mcp-deploy-verified", "mcp-external-untrusted"} <= ids

        body = {
            "mcp_server_id": "mcp-github-enterprise",
            "display_name": "GitHub Enterprise MCP",
            "transport": "streamable_http",
            "endpoint": "https://github.example.com/mcp",
            "trust_tier": "verified",
        }
        assert client.post("/v1/control-plane/mcp-servers", json=body, headers=viewer).status_code == 403
        created = client.post("/v1/control-plane/mcp-servers", json=body, headers=admin)
        assert created.status_code == 200
        assert created.json()["is_external_network"] is True
        assert created.json()["source"] == "custom"

        relisted = {item["mcp_server_id"]: item for item in client.get("/v1/control-plane/mcp-servers", headers=admin).json()}
        assert relisted["mcp-github-enterprise"]["transport"] == "streamable_http"
        tools = client.get("/v1/control-plane/tools", headers=admin).json()
        assert not any(item["tool_name"].startswith("__mcp_server__:") for item in tools)

        assert client.post("/v1/control-plane/mcp-servers/mcp-github-enterprise/breaker/open", headers=viewer).status_code == 403
        opened = client.post("/v1/control-plane/mcp-servers/mcp-github-enterprise/breaker/open", headers=admin)
        assert opened.status_code == 200 and opened.json()["is_open"] is True
        assert client.get("/v1/control-plane/mcp-servers", headers=admin).json()[-1]["breaker_open"] is True
        closed = client.post("/v1/control-plane/mcp-servers/mcp-github-enterprise/breaker/close", headers=admin)
        assert closed.status_code == 200 and closed.json()["is_open"] is False
        assert client.post("/v1/control-plane/mcp-servers/does-not-exist/breaker/open", headers=admin).status_code == 404

    events = _query(session_factory, select(GovernanceEvent.event_type))
    assert {"mcp_server.upserted", "mcp_breaker.opened", "mcp_breaker.closed"} <= set(events)


def test_v157_mcp_trust_tiers_and_external_network_transport_guardrails(client, session_factory):
    _register_tool(client, "ext_web_fetch", "mcp-external-untrusted")
    res = client.post("/v1/agent/submit", json=_payload("analysis", ["ext_web_fetch"]))
    body = res.json()
    assert body["status"] == "awaiting_approval"
    assert "mcp_untrusted_server:mcp-external-untrusted" in body["policy_flags"]
    assert "mcp_external_network_violation:mcp-external-untrusted" in body["policy_flags"]
    assert body["risk_score"] >= 0.45
    plan = client.get(f"/v1/runs/{body['run_id']}/execution-plan").json()
    gate_types = {gate["gate_type"] for gate in plan["approval_gates"]}
    assert {"mcp_trust_gate", "mcp_network_gate"} <= gate_types

    # verified external server is allowed when no_external_network=False
    _register_server(client, "mcp-ext-verified", transport="sse", endpoint="https://partner.example.com/sse")
    _register_tool(client, "partner_lookup", "mcp-ext-verified")
    blocked_net = client.post("/v1/agent/submit", json=_payload("analysis", ["partner_lookup"])).json()
    assert "mcp_external_network_violation:mcp-ext-verified" in blocked_net["policy_flags"]
    allowed_net = client.post(
        "/v1/agent/submit", json=_payload("analysis", ["partner_lookup"], no_external_network=False)
    ).json()
    assert not any(flag.startswith("mcp_") for flag in allowed_net["policy_flags"])

    # blocked tier -> denied, zero cost & zero lease
    _register_server(client, "mcp-ext-verified", transport="sse", endpoint="https://partner.example.com/sse", trust_tier="blocked")
    denied = client.post("/v1/agent/submit", json=_payload("analysis", ["partner_lookup"], no_external_network=False)).json()
    assert denied["status"] == "denied"
    assert "mcp_server_blocked:mcp-ext-verified" in denied["policy_flags"]
    costs = _query(session_factory, select(CostEvent).where(CostEvent.run_id == denied["run_id"]))
    assert costs == []
    assert client.get(f"/v1/cost/runs/{denied['run_id']}").json()["token_lease_status"].startswith("none")

    # tenant allow-list
    _register_server(client, "mcp-tenant-only", allowed_tenants=["other-tenant"])
    _register_tool(client, "tenant_only_tool", "mcp-tenant-only")
    not_allowed = client.post("/v1/agent/submit", json=_payload("analysis", ["tenant_only_tool"])).json()
    assert not_allowed["status"] == "denied"
    assert "mcp_tenant_not_allowed:mcp-tenant-only" in not_allowed["policy_flags"]


def test_v157_tool_json_schema_validation_and_injection_blocking(client, session_factory):
    ok = client.post(
        "/v1/control-plane/tools/query_data/validate-arguments",
        json={"arguments": {"query": "SELECT 1", "limit": 10}},
    ).json()
    assert ok["valid"] is True and ok["mcp_server_id"] == "mcp-data-verified"

    bad = client.post(
        "/v1/control-plane/tools/query_data/validate-arguments",
        json={"arguments": {"limit": "ten", "undeclared_field": "x"}},
    ).json()
    assert bad["valid"] is False
    joined = " ".join(bad["errors"])
    assert "required field missing" in joined and "expected type integer" in joined and "additional property" in joined

    smuggle = client.post(
        "/v1/agent/submit",
        json=_payload("data_query", ["query_data"], args={"query_data": {"query": "SELECT 1", "undeclared_field": "hack"}}),
    ).json()
    assert smuggle["status"] == "denied"
    assert "mcp_tool_schema_violation:query_data" in smuggle["policy_flags"]

    traversal = client.post(
        "/v1/agent/submit",
        json=_payload("code_fix", ["write_patch"], args={"write_patch": {"file_path": "../../etc/passwd", "patch_diff": "+root"}}),
    ).json()
    assert traversal["status"] == "denied"
    assert "mcp_argument_injection_detected:write_patch" in traversal["policy_flags"]

    shell = client.post(
        "/v1/agent/submit",
        json=_payload("code_fix", ["read_repo"], args={"read_repo": {"repo_path": "app; rm -rf /"}}),
    ).json()
    assert shell["status"] == "denied"
    assert "mcp_argument_injection_detected:read_repo" in shell["policy_flags"]

    sql = client.post(
        "/v1/agent/submit",
        json=_payload("data_query", ["query_data"], args={"query_data": {"query": "SELECT 1; DROP TABLE users"}}),
    ).json()
    assert sql["status"] == "denied"
    assert "mcp_argument_injection_detected:query_data" in sql["policy_flags"]

    incidents = _query(session_factory, select(Incident).where(Incident.code == "mcp_argument_injection"))
    assert {item.run_id for item in incidents} >= {traversal["run_id"], shell["run_id"], sql["run_id"]}
    assert all(item.severity == "high" for item in incidents)
    schema_events = _query(
        session_factory, select(GovernanceEvent).where(GovernanceEvent.event_type == "mcp_schema_violation")
    )
    assert any(event.run_id == smuggle["run_id"] and event.decision == "blocked" for event in schema_events)
    assert _query(session_factory, select(CostEvent).where(CostEvent.run_id == smuggle["run_id"])) == []

    # escalate mode routes schema violations to approval instead of denial
    _register_tool(
        client,
        "escalating_tool",
        "mcp-core-local",
        metadata={
            "schema_enforcement": "escalate",
            "input_schema": {"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}}},
        },
    )
    escalated = client.post(
        "/v1/agent/submit", json=_payload("analysis", ["escalating_tool"], args={"escalating_tool": {"other": 1}})
    ).json()
    assert escalated["status"] == "awaiting_approval"
    assert "mcp_tool_schema_violation:escalating_tool" in escalated["policy_flags"]


def test_v157_sensitive_argument_redaction_across_audit_trail(client, session_factory):
    args = {
        "query_data": {
            "query": "SELECT * FROM metrics WHERE owner = 'ops@example.com'",
            "limit": 10,
            "api_key": FAKE_API_KEY,
        }
    }
    res = client.post("/v1/agent/submit", json=_payload("data_query", ["query_data"], args=args))
    body = res.json()
    assert body["status"] == "queued", body
    run_id = body["run_id"]
    _run_worker(session_factory, run_id, client)

    run = client.get(f"/v1/runs/{run_id}").json()
    plan = client.get(f"/v1/runs/{run_id}/execution-plan").json()
    tool_execs = client.get(f"/v1/runs/{run_id}/tool-executions").json()
    bundle = client.get(f"/v1/runs/{run_id}/replay-bundle").json()
    assert run["status"] == "completed"
    assert run["request_payload"]["tool_arguments"]["query_data"]["api_key"] == "[REDACTED]"
    assert "[REDACTED_EMAIL]" in run["request_payload"]["tool_arguments"]["query_data"]["query"]
    selected = plan["selected_tools"][0]
    assert selected["sanitized_arguments"]["api_key"] == "[REDACTED]"
    assert "query_data.api_key" in selected["redacted_fields"]
    assert selected["mcp_server_id"] == "mcp-data-verified"
    assert tool_execs[0]["sandbox_policy"]["sanitized_arguments"]["api_key"] == "[REDACTED]"
    assert tool_execs[0]["sandbox_policy"]["schema_validation"] == "passed"
    for blob in (run, plan, tool_execs, bundle):
        assert FAKE_API_KEY not in json.dumps(blob)
        assert "ops@example.com" not in json.dumps(blob)

    stored = _query(session_factory, select(AgentRun.request_payload).where(AgentRun.run_id == run_id))
    plans = _query(session_factory, select(ExecutionPlan.selected_tools).where(ExecutionPlan.run_id == run_id))
    execs = _query(session_factory, select(ToolExecution.sandbox_policy).where(ToolExecution.run_id == run_id))
    assert FAKE_API_KEY not in json.dumps(stored) + json.dumps(plans) + json.dumps(execs)

    cleaned, redacted = sanitize_tool_arguments(
        {"headers": {"Authorization": "Bearer abc.def"}, "note": "token Bearer xyz123", "password": "p"},
        tool_name="unit",
    )
    assert cleaned["headers"]["Authorization"] == "[REDACTED]"
    assert cleaned["password"] == "[REDACTED]"
    assert "xyz123" not in cleaned["note"]
    assert len(redacted) == 3

    metrics = client.get("/v1/metrics").text
    assert "agent_mcp_argument_redactions_total" in metrics


def test_v157_dynamic_tool_registration_executes_in_worker_without_false_block(client, session_factory):
    _register_tool(client, "mcp_custom_analyzer", "mcp-core-local", task_types=["analysis"])
    res = client.post("/v1/agent/submit", json=_payload("analysis", ["mcp_custom_analyzer"]))
    body = res.json()
    assert body["status"] == "queued", body
    _run_worker(session_factory, body["run_id"], client)
    run = client.get(f"/v1/runs/{body['run_id']}").json()
    execs = client.get(f"/v1/runs/{body['run_id']}/tool-executions").json()
    assert run["status"] == "completed"
    assert execs[0]["tool_name"] == "mcp_custom_analyzer"
    assert execs[0]["status"] == "simulated"
    assert execs[0]["sandbox_policy"]["mcp_server_id"] == "mcp-core-local"


def test_v157_finops_mcp_server_id_and_tool_cost_attribution(client, session_factory):
    tenant = "v157-finops"
    res = client.post("/v1/agent/submit", json=_payload("data_query", ["query_data", "read_data"], tenant=tenant))
    run_id = res.json()["run_id"]
    assert res.json()["status"] == "queued"
    _run_worker(session_factory, run_id, client)

    usage = _query(session_factory, select(UsageEvent).where(UsageEvent.run_id == run_id, UsageEvent.step_id == "step_2_tools"))
    assert usage and all(item.mcp_server_id == "mcp-data-verified" for item in usage)
    assert all(item.tool_id == "query_data" and item.tool_name == "query_data" for item in usage)
    assert all(item.tool_risk_level == "medium" and item.tool_provider == "mcp-data-verified" for item in usage)

    summary = client.get(f"/v1/cost/runs/{run_id}").json()
    assert set(summary["tool_breakdown"]) == {"query_data", "read_data"}
    assert set(summary["mcp_server_breakdown"]) == {"mcp-data-verified", "mcp-core-local"}
    tool_total = sum(Decimal(v) for v in summary["tool_breakdown"].values())
    mcp_total = sum(Decimal(v) for v in summary["mcp_server_breakdown"].values())
    settled_tool = sum(
        Decimal(ce["pre_tax_base_cost"]) for ce in summary["cost_events"] if ce["resource_type"] == "tool" and ce["is_settled"]
    )
    assert tool_total == mcp_total == settled_tool
    tool_costs = [ce for ce in summary["cost_events"] if ce["resource_type"] == "tool"]
    assert all(ce["mcp_server_id"] == "mcp-data-verified" and ce["tool_id"] == "query_data" for ce in tool_costs)
    assert all(ce["allocation_tags"]["mcp_server_id"] == "mcp-data-verified" for ce in tool_costs)

    tenant_summary = client.get(f"/v1/cost/tenants/{tenant}").json()
    top_tool_ids = [item["tool_id"] for item in tenant_summary["top_tools"]]
    assert "query_data" in top_tool_ids
    assert "step_1_plan" not in top_tool_ids and "worker_runtime" not in top_tool_ids
    assert {item["mcp_server_id"] for item in tenant_summary["top_mcp_servers"]} == {"mcp-data-verified", "mcp-core-local"}
    filtered = client.get(f"/v1/cost/tenants/{tenant}?mcp_server_id=mcp-core-local").json()
    assert set(filtered["mcp_server_breakdown"]) <= {"mcp-data-verified", "mcp-core-local"}

    export_json = client.get(f"/v1/cost/tenants/{tenant}/export?format=json").json()
    assert "mcp-data-verified" in export_json["mcp_server_breakdown"]
    export_csv = client.get(f"/v1/cost/tenants/{tenant}/export?format=csv").text
    header = export_csv.splitlines()[0].split(",")
    assert header[:2] == ["cost_id", "usage_event_id"]
    assert header[-2:] == ["mcp_server_id", "tool_id"]
    assert len(CSV_EXPORT_COLUMNS) == 37
    assert "0E-8" not in export_csv
    only_data = client.get(f"/v1/cost/tenants/{tenant}/export?format=json&mcp_server_id=mcp-data-verified").json()
    assert only_data["items"] and all(item["resource_type"] == "tool" for item in only_data["items"])


def test_v157_budget_policy_mcp_server_and_specific_tool_scopes(client):
    tenant = "v157-budget"
    created = client.post(
        "/v1/budgets",
        json={
            "policy_name": "Data MCP Cap",
            "tenant_id": tenant,
            "scope": "mcp_server",
            "scope_id": "mcp-data-verified",
            "period": "daily",
            "hard_limit_amount": "0.000001",
            "actions_on_hard_limit": ["record_violation", "fail_closed"],
        },
    )
    assert created.status_code == 200
    data_run = client.post("/v1/agent/submit", json=_payload("data_query", ["query_data"], tenant=tenant)).json()
    assert data_run["status"] == "denied"
    assert "finops_budget_hard_limit_exceeded" in data_run["policy_flags"]
    core_run = client.post("/v1/agent/submit", json=_payload("code_fix", ["read_repo"], tenant=tenant)).json()
    assert core_run["status"] == "queued"

    tenant2 = "v157-tool-budget"
    client.post(
        "/v1/budgets",
        json={
            "policy_name": "run_tests cap",
            "tenant_id": tenant2,
            "scope": "tool",
            "tool_id": "run_tests",
            "period": "daily",
            "hard_limit_amount": "0.000001",
            "actions_on_hard_limit": ["record_violation", "fail_closed"],
        },
    )
    r_tests = client.post("/v1/agent/submit", json=_payload("code_fix", ["run_tests"], tenant=tenant2)).json()
    assert r_tests["status"] == "denied"
    r_repo = client.post("/v1/agent/submit", json=_payload("code_fix", ["read_repo"], tenant=tenant2)).json()
    assert r_repo["status"] == "queued"
    forecast = client.get(f"/v1/cost/tenants/{tenant}/forecast")
    assert forecast.status_code == 200


def test_v157_per_mcp_server_fixed_window_quota(client):
    tenant = "v157-quota"
    _register_server(client, "mcp-quota-small", quota_per_minute=2)
    _register_tool(client, "quota_tool_a", "mcp-quota-small")
    _register_tool(client, "quota_tool_b", "mcp-quota-small")
    first = client.post("/v1/agent/submit", json=_payload("analysis", ["quota_tool_a", "quota_tool_b"], tenant=tenant))
    assert first.status_code == 200
    second = client.post("/v1/agent/submit", json=_payload("analysis", ["quota_tool_a"], tenant=tenant))
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "mcp_server_quota_exceeded"
    summary = client.get(f"/v1/governance/summary?tenant_id={tenant}")
    assert summary.status_code == 200
    assert summary.json()["quota_limited"] >= 1

    page = client.post(
        "/dashboard/playground",
        data={
            "tenant_id": tenant,
            "user_id": "v157-user",
            "task_type": "analysis",
            "allowed_tools": "quota_tool_b",
            "prompt": "Analyze",
            "goal": "Summarize",
            "max_tool_calls": "4",
            "no_external_network": "on",
        },
    )
    assert page.status_code == 200
    assert "mcp_server_quota_exceeded" in page.text


def test_v157_dashboard_mcp_control_plane_playground_and_costs_ui(client, session_factory):
    r = client.post(
        "/dashboard/control-plane/mcp-servers",
        data={
            "mcp_server_id": "mcp-qa-v157",
            "display_name": "QA Verified MCP Server",
            "transport": "sse",
            "endpoint": "https://mcp.internal.local/qa",
            "trust_tier": "verified",
            "quota_per_minute": "45",
        },
        follow_redirects=True,
    )
    assert r.status_code == 200
    assert "mcp-qa-v157" in r.text
    r = client.post(
        "/dashboard/control-plane/tools",
        data={
            "tool_name": "qa_mcp_tool",
            "display_name": "QA MCP Tool",
            "task_types": "analysis",
            "risk_level": "low",
            "mcp_server_id": "mcp-qa-v157",
            "required_args": "query",
            "sensitive_args": "api_token",
        },
        follow_redirects=True,
    )
    assert r.status_code == 200 and "qa_mcp_tool" in r.text
    r = client.post("/dashboard/control-plane/mcp-servers/mcp-qa-v157/breaker/open", data={}, follow_redirects=True)
    assert "Reset MCP Breaker" in r.text
    r = client.post("/dashboard/control-plane/mcp-servers/mcp-qa-v157/breaker/close", data={}, follow_redirects=True)
    assert "Trip MCP Breaker" in r.text
    bad = client.post("/dashboard/control-plane/tools", data={"tool_name": "__mcp_server__:evil"}, follow_redirects=True)
    assert bad.status_code == 200 and "reserved prefix" in bad.text

    page = client.post(
        "/dashboard/playground",
        data={
            "tenant_id": "finops-qa-tenant",
            "user_id": "v157-user",
            "task_type": "analysis",
            "allowed_tools": "qa_mcp_tool",
            "prompt": "Analyze",
            "goal": "Summarize",
            "max_tool_calls": "4",
            "no_external_network": "on",
            "tool_arguments": json.dumps({"qa_mcp_tool": {"query": "test", "api_token": "secret-xyz"}}),
        },
    )
    assert page.status_code == 200
    assert "secret-xyz" not in page.text
    assert "mcp-qa-v157" in page.text
    runs = _query(session_factory, select(AgentRun).where(AgentRun.tenant_id == "finops-qa-tenant"))
    run = runs[-1]
    assert run.request_payload["tool_arguments"]["qa_mcp_tool"]["api_token"] == "[REDACTED]"
    detail = client.get(f"/dashboard/runs/{run.run_id}")
    assert detail.status_code == 200 and "secret-xyz" not in detail.text
    assert "V1.5.7 FinOps ledger" in detail.text and "MCP Server Breakdown" in detail.text

    invalid = client.post(
        "/dashboard/playground",
        data={"tenant_id": "finops-qa-tenant", "user_id": "u", "task_type": "analysis", "prompt": "p", "goal": "g",
              "max_tool_calls": "4", "tool_arguments": "{not json"},
    )
    assert invalid.status_code == 200 and "tool_arguments must be valid JSON" in invalid.text

    costs = client.get("/dashboard/costs?tenant_id=finops-qa-tenant")
    assert costs.status_code == 200
    assert "V1.5.7 Agent FinOps Core" in costs.text and "MCP Server Cost Breakdown" in costs.text
    assert "mcp-qa-v157" in costs.text
    assert "0E-8" not in costs.text
    csv_export = client.get("/dashboard/costs/export?tenant_id=finops-qa-tenant&format=csv&mcp_server_id=mcp-qa-v157")
    assert csv_export.status_code == 200 and "mcp_server_id" in csv_export.text.splitlines()[0]

    ops = client.get("/dashboard/operations")
    assert ops.status_code == 200
    assert "mcp_gateway_registry_health" in ops.text and 'value="v1.5.7"' in ops.text
    readiness = client.get("/v1/operations/readiness").json()
    check = next(item for item in readiness["checks"] if item["check_key"] == "mcp_gateway_registry_health")
    assert check["status"] == "pass"
    client.post("/v1/control-plane/mcp-servers/mcp-qa-v157/breaker/open")
    readiness = client.get("/v1/operations/readiness").json()
    check = next(item for item in readiness["checks"] if item["check_key"] == "mcp_gateway_registry_health")
    assert check["status"] == "warn"
    client.post("/v1/control-plane/mcp-servers/mcp-qa-v157/breaker/close")


def test_v157_observability_metrics_and_version_anchors(client):
    metrics = client.get("/v1/metrics").text
    for name in (
        "agent_mcp_gateway_requests_total",
        "agent_mcp_schema_violations_total",
        "agent_mcp_argument_redactions_total",
    ):
        assert name in metrics
    assert FINOPS_CALCULATION_VERSION == "v1.5.7"
    from app.main import app as fastapi_app

    assert fastapi_app.version == "1.5.7"
    with open("pyproject.toml", encoding="utf-8") as fh:
        assert 'version = "1.5.7"' in fh.read()
    with open("scripts/security_check.py", encoding="utf-8") as fh:
        assert "(v1.5.7)" in fh.read()
    with open("app/ui/templates/operations.html", encoding="utf-8") as fh:
        assert 'value="v1.5.7"' in fh.read()


def test_v157_approved_untrusted_run_completes_in_worker_without_reescalation(client, session_factory):
    _register_tool(client, "ext_fetch_approved", "mcp-external-untrusted")
    body = client.post("/v1/agent/submit", json=_payload("analysis", ["ext_fetch_approved"])).json()
    assert body["status"] == "awaiting_approval"
    decision = client.post(
        f"/v1/approvals/{body['run_id']}/decision",
        json={"decision": "approve_once", "reviewer": "secops", "reason": "Reviewed third-party MCP usage"},
    )
    assert decision.status_code == 200 and decision.json()["status"] == "queued"
    _run_worker(session_factory, body["run_id"], client)
    run = client.get(f"/v1/runs/{body['run_id']}").json()
    assert run["status"] == "completed", run["decision_reason"]

    # TOCTOU: server blocked while the run sits in the queue -> worker denies & releases the lease
    _register_server(client, "mcp-toctou", transport="stdio")
    _register_tool(client, "toctou_tool", "mcp-toctou")
    queued = client.post("/v1/agent/submit", json=_payload("analysis", ["toctou_tool"], tenant="v157-toctou")).json()
    assert queued["status"] == "queued"
    _register_server(client, "mcp-toctou", transport="stdio", trust_tier="blocked")
    _run_worker(session_factory, queued["run_id"], client)
    denied = client.get(f"/v1/runs/{queued['run_id']}").json()
    assert denied["status"] == "denied"
    assert "mcp_server_blocked:mcp-toctou" in denied["policy_flags"]
    leases = client.get("/v1/cost/tenants/v157-toctou/leases").json()
    assert leases["active_lease_count"] == 0


def test_v157_scan_profiles_avoid_false_positives_and_payload_limits(client):
    union = client.post(
        "/v1/agent/submit",
        json=_payload("data_query", ["query_data"], args={"query_data": {"query": "SELECT a FROM t1 UNION SELECT a FROM t2", "limit": 10}}),
    ).json()
    assert union["status"] == "queued", union
    patch_ok = client.post(
        "/v1/agent/submit",
        json=_payload("code_fix", ["write_patch"], args={"write_patch": {"file_path": "app/main.py", "patch_diff": "+echo $(date)"}}),
    ).json()
    assert patch_ok["status"] == "queued", patch_ok

    undeclared = client.post(
        "/v1/agent/submit", json=_payload("analysis", ["read_data"], args={"query_data": {"query": "SELECT 1"}})
    ).json()
    assert undeclared["status"] == "denied"
    assert "mcp_tool_schema_violation:query_data" in undeclared["policy_flags"]

    nested: dict = {"v": 1}
    for _ in range(9):
        nested = {"n": nested}
    deep = client.post("/v1/agent/submit", json=_payload("analysis", ["read_data"], args={"read_data": nested})).json()
    assert deep["status"] == "denied" and "mcp_tool_schema_violation:read_data" in deep["policy_flags"]

    huge = client.post(
        "/v1/agent/submit", json=_payload("analysis", ["read_data"], args={"read_data": {"source": "x" * 70000}})
    ).json()
    assert huge["status"] == "denied" and "mcp_tool_schema_violation:read_data" in huge["policy_flags"]

    result = validate_tool_arguments("read_data", {"blob": "a" * 70000}, None, max_bytes=65536)
    assert "payload_too_large" in result.violation_types

    request = AgentRequest(user_id="u", prompt="Analyze data", goal="Summarize", allowed_tools=["read_data"])
    engine = PolicyEngine()
    baseline = engine.evaluate(request, [])
    with_none = engine.evaluate(request, [], mcp_context=None)
    assert baseline.model_dump() == with_none.model_dump()
    hard = engine.evaluate(request, [], mcp_context=McpGatewayContext(flags=["mcp_server_blocked:x"]))
    assert hard.decision.value == "deny"


def test_v157_registry_namespace_hostname_and_breaker_key_isolation(client):
    reserved = client.post(
        "/v1/control-plane/tools",
        json={"tool_name": "__mcp_server__:x", "display_name": "x", "task_types": ["analysis"]},
    )
    assert reserved.status_code == 422
    too_long = client.post(
        "/v1/control-plane/mcp-servers", json={"mcp_server_id": "a" * 114, "transport": "stdio"}
    )
    assert too_long.status_code == 422
    assert client.post("/v1/control-plane/mcp-servers", json={"mcp_server_id": "a" * 113}).status_code == 200

    evil = _register_server(client, "mcp-evil-localhost", transport="streamable_http", endpoint="http://localhost.evil.com/mcp")
    assert evil["is_external_network"] is True
    suffixes = Settings().mcp_internal_host_suffix_list
    assert is_external_endpoint("sse", "http://localhost.evil.com/mcp", suffixes) is True
    assert is_external_endpoint("sse", "not a url", suffixes) is True
    assert is_external_endpoint("sse", "https://data.internal.local/sse", suffixes) is False
    assert is_external_endpoint("sse", "https://evilinternal.local/sse", suffixes) is True
    assert is_external_endpoint("sse", "http://127.0.0.1:9000", suffixes) is False
    assert is_external_endpoint("stdio", "https://example.com", suffixes) is False

    client.post("/v1/control-plane/mcp-servers/mcp-core-local/breaker/open")
    tool_breakers = client.get("/v1/control-plane/breakers").json()
    assert all("mcp" not in item["tool_name"] for item in tool_breakers)
    client.post("/v1/control-plane/mcp-servers/mcp-core-local/breaker/close")

    async def _store_parity():
        store = InMemoryRunStateStore()
        await store.open_mcp_breaker("mcp-x", reason="t", ttl_seconds=60)
        assert await store.list_open_breakers() == {}
        assert "mcp-x" in await store.list_open_mcp_breakers()
        assert "mcp-x" in await store.get_open_mcp_breakers(["mcp-x", "mcp-y"])
        assert await store.close_mcp_breaker("mcp-x") is True
        assert await store.list_open_mcp_breakers() == {}

    asyncio.run(_store_parity())


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_v157_settings_validation_guards():
    with pytest.raises(ValueError):
        Settings(mcp_schema_enforcement_mode="ignore")
    with pytest.raises(ValueError):
        Settings(mcp_internal_host_suffixes="internal.local")
    rows = ToolDefinition.__table__.c.tool_name.type.length
    assert rows == 128


def test_v157_dashboard_governance_tenant_update_existing_regression(client):
    form = {"tenant_id": "v157-gov-tenant", "display_name": "First Name", "request_limit_per_minute": "60", "high_risk_limit_per_hour": "10"}
    first = client.post("/dashboard/governance/tenants", data=form, follow_redirects=True)
    assert first.status_code == 200 and "First Name" in first.text
    second = client.post(
        "/dashboard/governance/tenants", data={**form, "display_name": "Renamed Tenant"}, follow_redirects=True
    )
    assert second.status_code == 200
    assert "Renamed Tenant" in second.text
