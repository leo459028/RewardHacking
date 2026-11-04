from __future__ import annotations

import asyncio
import csv
import io
from decimal import Decimal
import pytest

from app.core.config import Settings, get_settings
from app.services.finops import FINOPS_CALCULATION_VERSION
from app.workers.consumer import process_run_by_id


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


def test_settled_ledger_eliminates_estimated_actual_double_counting(
    client, session_factory, low_risk_payload
):
    payload = {
        **low_risk_payload,
        "tenant_id": "settled-t",
        "metadata": {
            "provider": "internal",
            "model_name": "deterministic-control-plane",
            "project_id": "proj-settled",
            "cost_center": "cc-settled",
        },
    }
    res = client.post("/v1/agent/submit", json=payload)
    assert res.status_code == 200
    run_id = res.json()["run_id"]

    before = client.get(f"/v1/cost/runs/{run_id}").json()
    est_cost = Decimal(before["estimated_cost"])
    assert est_cost > Decimal("0")
    assert Decimal(before["settled_cost"]) == est_cost
    assert all(ce["is_settled"] is True for ce in before["cost_events"])

    # Execute worker actual usage
    _run_worker(session_factory, run_id, client)

    after = client.get(f"/v1/cost/runs/{run_id}").json()
    act_cost = Decimal(after["actual_cost"])
    settled_cost = Decimal(after["settled_cost"])
    assert act_cost > Decimal("0")
    # Settled cost must NOT double count superseded estimated events (step_2_tools, model/tool)
    assert settled_cost < (est_cost + act_cost)

    by_unit_settled = [ce for ce in after["cost_events"] if ce["is_settled"]]
    by_unit_superseded = [ce for ce in after["cost_events"] if not ce["is_settled"]]
    # step_2_tools estimated is superseded by actual tool_call
    assert any(
        ce["cost_type"] == "estimated" and ce["usage_unit"] == "tool_call"
        for ce in by_unit_superseded
    )
    # step_1_plan input_tokens and step_3_replay_bundle artifact_mb remain settled because worker only records output_tokens, tool_call, worker_seconds
    assert any(
        ce["cost_type"] == "estimated" and ce["usage_unit"] == "input_tokens"
        for ce in by_unit_settled
    )

    tenant_summary = client.get("/v1/cost/tenants/settled-t").json()
    assert Decimal(tenant_summary["month_to_date_cost"]) == settled_cost
    assert Decimal(tenant_summary["today_cost"]) == settled_cost


def test_budget_enforcement_uses_settled_pre_tax_cost(
    client, session_factory, low_risk_payload
):
    # Create a TWD pricing item with 5% tax (exclusive) -> net=100 TWD, tax=5 TWD, fx=0.031
    # Pre-tax base = 100 * 0.031 = 3.10 USD; Billed base (with tax) = 105 * 0.031 = 3.255 USD
    p_res = client.post(
        "/v1/pricing",
        json={
            "provider": "custom",
            "model_name": "twd-budget-model",
            "resource_type": "model",
            "resource_subtype": "chat_completion",
            "price_unit": "per_call",
            "unit_price": "100",
            "currency": "TWD",
            "tax_included": False,
            "metadata": {"tax_rate": "0.05"},
        },
    )
    assert p_res.status_code == 200

    # Set hard limit to 3.20 USD:
    # Pre-tax base cost (3.10 USD) is BELOW 3.20 USD -> should ALLOW
    # If tax were included (3.255 USD), it would wrongly fail_closed!
    b_res = client.post(
        "/v1/budgets",
        json={
            "tenant_id": "pretax-budget-t",
            "policy_name": "Pre-Tax USD Budget",
            "scope": "tenant",
            "scope_id": "pretax-budget-t",
            "period": "monthly",
            "hard_limit_amount": "3.20",
            "currency": "USD",
            "budget_basis": "actual",
            "actions_on_hard_limit": ["record_violation", "fail_closed"],
        },
    )
    assert b_res.status_code == 200

    u_res = client.post(
        "/v1/usage-events",
        json={
            "tenant_id": "pretax-budget-t",
            "run_id": "run_pretax_1",
            "provider": "custom",
            "model_name": "twd-budget-model",
            "resource_type": "model",
            "resource_subtype": "chat_completion",
            "usage_unit": "per_call",
            "quantity": "1",
            "phase": "actual",
            "status": "success",
        },
    )
    assert u_res.status_code == 200

    # Submit a tiny run in the same tenant to trigger evaluate_budget_policies
    sub = client.post(
        "/v1/agent/submit",
        json={**low_risk_payload, "tenant_id": "pretax-budget-t"},
    )
    assert sub.status_code == 200
    assert sub.json()["status"] == "queued"

    # Now add a budget policy with budget_basis="base_currency" (which uses billed base cost 3.255 USD > 3.20 USD)
    b2_res = client.post(
        "/v1/budgets",
        json={
            "tenant_id": "pretax-budget-t",
            "policy_name": "Billed Base Currency Budget",
            "scope": "tenant",
            "scope_id": "pretax-budget-t",
            "period": "monthly",
            "hard_limit_amount": "3.20",
            "currency": "USD",
            "budget_basis": "base_currency",
            "actions_on_hard_limit": ["record_violation", "fail_closed"],
        },
    )
    assert b2_res.status_code == 200

    sub2 = client.post(
        "/v1/agent/submit",
        json={**low_risk_payload, "tenant_id": "pretax-budget-t"},
    )
    assert sub2.status_code == 200
    assert sub2.json()["status"] == "denied"
    assert "finops_budget_hard_limit_exceeded" in sub2.json()["policy_flags"]


def test_fx_conversion_and_tax_exclusive_inclusive_and_override(client):
    # 1. TWD Tax Exclusive (tax_included=False, tax_rate=0.05, default TWD fx=0.031)
    p1 = client.post(
        "/v1/pricing",
        json={
            "provider": "custom",
            "model_name": "tw-excl",
            "resource_type": "model",
            "resource_subtype": "chat_completion",
            "price_unit": "per_call",
            "unit_price": "100",
            "currency": "TWD",
            "tax_included": False,
            "metadata": {"tax_rate": "0.05"},
        },
    )
    assert p1.status_code == 200

    client.post(
        "/v1/usage-events",
        json={
            "tenant_id": "fx-t",
            "run_id": "run_fx_excl",
            "provider": "custom",
            "model_name": "tw-excl",
            "resource_type": "model",
            "resource_subtype": "chat_completion",
            "usage_unit": "per_call",
            "quantity": "2",
            "phase": "actual",
            "status": "success",
        },
    )
    c1 = client.get("/v1/cost/runs/run_fx_excl").json()
    ce1 = c1["cost_events"][0]
    assert Decimal(ce1["net_cost"]) == Decimal("200")
    assert Decimal(ce1["tax_amount"]) == Decimal("10")
    assert Decimal(ce1["fx_rate"]) == Decimal("0.031")
    assert ce1["fx_rate_source"] == "configured_fx_table"
    assert Decimal(ce1["pre_tax_base_cost"]) == Decimal("6.2")
    assert Decimal(ce1["base_currency_cost"]) == Decimal("6.51")
    assert ce1["calculation_version"] == FINOPS_CALCULATION_VERSION

    # 2. TWD Tax Inclusive (tax_included=True, tax_rate=0.05, gross=105 TWD -> pre_tax=100, tax=5)
    p2 = client.post(
        "/v1/pricing",
        json={
            "provider": "custom",
            "model_name": "tw-incl",
            "resource_type": "model",
            "resource_subtype": "chat_completion",
            "price_unit": "per_call",
            "unit_price": "105",
            "currency": "TWD",
            "tax_included": True,
            "metadata": {"tax_rate": "0.05"},
        },
    )
    assert p2.status_code == 200

    client.post(
        "/v1/usage-events",
        json={
            "tenant_id": "fx-t",
            "run_id": "run_fx_incl",
            "provider": "custom",
            "model_name": "tw-incl",
            "resource_type": "model",
            "resource_subtype": "chat_completion",
            "usage_unit": "per_call",
            "quantity": "1",
            "phase": "actual",
            "status": "success",
        },
    )
    c2 = client.get("/v1/cost/runs/run_fx_incl").json()
    ce2 = c2["cost_events"][0]
    assert Decimal(ce2["net_cost"]) == Decimal("100")
    assert Decimal(ce2["tax_amount"]) == Decimal("5")
    assert Decimal(ce2["pre_tax_base_cost"]) == Decimal("3.1")
    assert Decimal(ce2["base_currency_cost"]) == Decimal("3.255")

    # 3. Pricing Metadata FX Override (fx_rate=0.032) + CNY check
    p3 = client.post(
        "/v1/pricing",
        json={
            "provider": "custom",
            "model_name": "tw-override",
            "resource_type": "model",
            "resource_subtype": "chat_completion",
            "price_unit": "per_call",
            "unit_price": "100",
            "currency": "TWD",
            "tax_included": False,
            "metadata": {"fx_rate": "0.032", "tax_rate": "0"},
        },
    )
    assert p3.status_code == 200

    client.post(
        "/v1/usage-events",
        json={
            "tenant_id": "fx-t",
            "run_id": "run_fx_override",
            "provider": "custom",
            "model_name": "tw-override",
            "resource_type": "model",
            "resource_subtype": "chat_completion",
            "usage_unit": "per_call",
            "quantity": "1",
            "phase": "actual",
            "status": "success",
        },
    )
    c3 = client.get("/v1/cost/runs/run_fx_override").json()
    ce3 = c3["cost_events"][0]
    assert Decimal(ce3["fx_rate"]) == Decimal("0.032")
    assert ce3["fx_rate_source"] == "pricing_override"
    assert Decimal(ce3["pre_tax_base_cost"]) == Decimal("3.2")


def test_same_currency_usd_fx_identity_and_zero_tax(client, low_risk_payload):
    res = client.post(
        "/v1/agent/submit",
        json={**low_risk_payload, "tenant_id": "usd-identity-t"},
    )
    assert res.status_code == 200
    run_id = res.json()["run_id"]

    summary = client.get(f"/v1/cost/runs/{run_id}").json()
    assert summary["currency"] == "USD"
    assert summary["base_currency"] == "USD"
    assert Decimal(summary["total_tax_amount"]) == Decimal("0")
    assert Decimal(summary["settled_cost"]) == Decimal(summary["total_billed_base_cost"])
    for ce in summary["cost_events"]:
        assert Decimal(ce["fx_rate"]) == Decimal("1")
        assert ce["fx_rate_source"] == "same_currency"
        assert Decimal(ce["tax_amount"]) == Decimal("0")
        assert Decimal(ce["pre_tax_base_cost"]) == Decimal(ce["base_currency_cost"])


def test_untrusted_usage_metadata_cannot_override_fx_or_tax(client):
    p_res = client.post(
        "/v1/pricing",
        json={
            "provider": "custom",
            "model_name": "tw-sec-model",
            "resource_type": "model",
            "resource_subtype": "chat_completion",
            "price_unit": "per_call",
            "unit_price": "100",
            "currency": "TWD",
            "tax_included": False,
            "metadata": {"tax_rate": "0.05"},
        },
    )
    assert p_res.status_code == 200

    # Operator tries to pass tiny fx_rate and zero tax_rate in UsageEventCreateRequest.metadata
    u_res = client.post(
        "/v1/usage-events",
        json={
            "tenant_id": "sec-fx-t",
            "run_id": "run_sec_fx",
            "provider": "custom",
            "model_name": "tw-sec-model",
            "resource_type": "model",
            "resource_subtype": "chat_completion",
            "usage_unit": "per_call",
            "quantity": "1",
            "phase": "actual",
            "status": "success",
            "metadata": {"fx_rate": "0.000001", "tax_rate": "0"},
        },
    )
    assert u_res.status_code == 200

    cost_res = client.get("/v1/cost/runs/run_sec_fx").json()
    ce = cost_res["cost_events"][0]
    # Must ignore untrusted usage overrides and use configured_fx_table 0.031 and pricing tax_rate 0.05
    assert Decimal(ce["fx_rate"]) == Decimal("0.031")
    assert ce["fx_rate_source"] == "configured_fx_table"
    assert Decimal(ce["tax_amount"]) == Decimal("5")
    ignored = ce["metadata"].get("ignored_untrusted_overrides", [])
    assert "fx_rate" in ignored
    assert "tax_rate" in ignored


def test_missing_fx_rate_records_violation_and_budget_currency_validation(client):
    # 1. Create pricing item with unsupported currency KRW
    p_res = client.post(
        "/v1/pricing",
        json={
            "provider": "custom",
            "model_name": "krw-model",
            "resource_type": "model",
            "resource_subtype": "chat_completion",
            "price_unit": "per_call",
            "unit_price": "1000",
            "currency": "KRW",
        },
    )
    assert p_res.status_code == 200

    u_res = client.post(
        "/v1/usage-events",
        json={
            "tenant_id": "krw-t",
            "run_id": "run_krw_1",
            "provider": "custom",
            "model_name": "krw-model",
            "resource_type": "model",
            "resource_subtype": "chat_completion",
            "usage_unit": "per_call",
            "quantity": "1",
            "phase": "actual",
            "status": "success",
        },
    )
    assert u_res.status_code == 200

    cost_res = client.get("/v1/cost/runs/run_krw_1").json()
    ce = cost_res["cost_events"][0]
    assert ce["fx_rate_source"] == "missing_fx_rate"

    violations = client.get("/v1/budget-violations", params={"tenant_id": "krw-t"}).json()
    assert any(v["violation_type"] == "missing_fx_rate" for v in violations)

    # 2. BudgetPolicyCreateRequest with unsupported currency KRW -> 422
    b_bad = client.post(
        "/v1/budgets",
        json={
            "tenant_id": "krw-t",
            "policy_name": "Invalid KRW Budget",
            "scope": "tenant",
            "scope_id": "krw-t",
            "period": "monthly",
            "hard_limit_amount": "10000",
            "currency": "KRW",
        },
    )
    assert b_bad.status_code == 422

    # 3. BudgetPolicyCreateRequest with supported currency TWD -> 200 and converts threshold
    b_twd = client.post(
        "/v1/budgets",
        json={
            "tenant_id": "twd-pol-t",
            "policy_name": "TWD Policy",
            "scope": "tenant",
            "scope_id": "twd-pol-t",
            "period": "monthly",
            "hard_limit_amount": "320",
            "currency": "TWD",
        },
    )
    assert b_twd.status_code == 200

    # Startup settings validator rejects zero/negative FX rate
    with pytest.raises(ValueError, match="FX rate for TWD must be > 0"):
        Settings(finops_fx_rates={"USD": Decimal("1"), "TWD": Decimal("0")})


def test_cost_center_and_allocation_tags_end_to_end_propagation(
    client, session_factory, low_risk_payload
):
    payload = {
        **low_risk_payload,
        "tenant_id": "alloc-t",
        "metadata": {
            "project_id": "proj-alpha",
            "cost_center": "cc-platform-01",
            "org_id": "org-apac",
            "environment": "staging",
            "allocation_tags": {"team": "guardrails", "tier": "gold"},
        },
    }
    res = client.post("/v1/agent/submit", json=payload)
    assert res.status_code == 200
    run_id = res.json()["run_id"]

    plan = client.get(f"/v1/runs/{run_id}/execution-plan").json()
    alloc = plan["model_route"]["allocation"]
    assert alloc["cost_center"] == "cc-platform-01"
    assert alloc["org_id"] == "org-apac"
    assert alloc["project_id"] == "proj-alpha"
    assert alloc["environment"] == "staging"
    assert alloc["allocation_tags"] == {"team": "guardrails", "tier": "gold"}

    _run_worker(session_factory, run_id, client)

    run_cost = client.get(f"/v1/cost/runs/{run_id}").json()
    for ue in run_cost["usage_events"]:
        assert ue["cost_center"] == "cc-platform-01"
        assert ue["org_id"] == "org-apac"
        assert ue["project_id"] == "proj-alpha"
        assert ue["environment"] == "staging"

    for ce in run_cost["cost_events"]:
        assert ce["cost_center"] == "cc-platform-01"
        assert ce["org_id"] == "org-apac"
        assert ce["project_id"] == "proj-alpha"
        assert ce["environment"] == "staging"
        assert ce["allocation_tags"]["cost_center"] == "cc-platform-01"
        assert ce["allocation_tags"]["custom"] == {"team": "guardrails", "tier": "gold"}

    t_summary = client.get(
        "/v1/cost/tenants/alloc-t",
        params={"project_id": "proj-alpha", "cost_center": "cc-platform-01"},
    ).json()
    assert "cc-platform-01" in t_summary["cost_center_breakdown"]
    assert Decimal(t_summary["cost_center_breakdown"]["cc-platform-01"]) == Decimal(
        run_cost["settled_cost"]
    )

    # Filter with non-matching cost_center returns 0
    empty_summary = client.get(
        "/v1/cost/tenants/alloc-t",
        params={"cost_center": "cc-non-existent"},
    ).json()
    assert Decimal(empty_summary["month_to_date_cost"]) == Decimal("0")


def test_allocation_tag_validation_truncates_and_warns_without_blocking(
    client, low_risk_payload
):
    many_tags = {f"key_{i:02d}": "valid_val" for i in range(25)}
    many_tags["bad_val_key"] = "v" * 200
    payload = {
        **low_risk_payload,
        "tenant_id": "warn-alloc-t",
        "metadata": {
            "cost_center": "bad cost center with spaces & symbols!" * 3,
            "allocation_tags": many_tags,
        },
    }
    res = client.post("/v1/agent/submit", json=payload)
    assert res.status_code == 200
    run_id = res.json()["run_id"]

    plan = client.get(f"/v1/runs/{run_id}/execution-plan").json()
    warnings = plan["trace_metadata"].get("attribution_warnings", [])
    assert len(warnings) >= 2
    alloc_tags = plan["model_route"]["allocation"]["allocation_tags"]
    assert len(alloc_tags) == 20
    assert all(len(v) <= 128 for v in alloc_tags.values())


def test_cost_adjustments_credit_debit_idempotency_rbac_and_budget_isolation(
    client, monkeypatch, low_risk_payload
):
    # Create a baseline run and tight budget (hard limit = 1.00 USD)
    b_res = client.post(
        "/v1/budgets",
        json={
            "tenant_id": "adj-t",
            "policy_name": "Adjustment Isolation Budget",
            "scope": "tenant",
            "scope_id": "adj-t",
            "period": "monthly",
            "hard_limit_amount": "1.00",
            "currency": "USD",
            "actions_on_hard_limit": ["record_violation", "fail_closed"],
        },
    )
    assert b_res.status_code == 200

    sub = client.post(
        "/v1/agent/submit",
        json={**low_risk_payload, "tenant_id": "adj-t"},
    )
    assert sub.status_code == 200
    run_id = sub.json()["run_id"]
    run_cost = client.get(f"/v1/cost/runs/{run_id}").json()
    first_cost_id = run_cost["cost_events"][0]["cost_id"]

    # Record a large debit adjustment (+50.00 USD) and a credit adjustment (-0.0005 USD)
    adj_debit = client.post(
        "/v1/cost/adjustments",
        json={
            "tenant_id": "adj-t",
            "amount": "50.00",
            "currency": "USD",
            "provider_invoice_id": "INV-2026-10-DEBIT",
            "reason": "Enterprise support fee",
            "idempotency_key": "idem-debit-1",
        },
    )
    assert adj_debit.status_code == 200
    assert adj_debit.json()["cost_id"].startswith("cost_adj_")

    # Idempotency check: repeating same idempotency_key returns identical cost_id without duplicating
    adj_debit_dup = client.post(
        "/v1/cost/adjustments",
        json={
            "tenant_id": "adj-t",
            "amount": "50.00",
            "currency": "USD",
            "provider_invoice_id": "INV-2026-10-DEBIT",
            "reason": "Enterprise support fee",
            "idempotency_key": "idem-debit-1",
        },
    )
    assert adj_debit_dup.status_code == 200
    assert adj_debit_dup.json()["cost_id"] == adj_debit.json()["cost_id"]

    # Reversal adjustment referencing existing cost_id
    adj_credit = client.post(
        "/v1/cost/adjustments",
        json={
            "tenant_id": "adj-t",
            "amount": "-10.00",
            "currency": "USD",
            "provider_invoice_id": "INV-2026-10-CREDIT",
            "reverses_cost_id": first_cost_id,
            "reason": "SLA credit",
        },
    )
    assert adj_credit.status_code == 200

    # Invalid reverses_cost_id -> 404
    bad_rev = client.post(
        "/v1/cost/adjustments",
        json={
            "tenant_id": "adj-t",
            "amount": "-1.00",
            "currency": "USD",
            "provider_invoice_id": "INV-BAD-REV",
            "reverses_cost_id": "cost_non_existent",
            "reason": "Bad reversal",
        },
    )
    assert bad_rev.status_code == 404

    # Verify tenant summary includes adjustment_total (+40.00) and reconciled_month_to_date_cost
    t_sum = client.get("/v1/cost/tenants/adj-t").json()
    assert Decimal(t_sum["adjustment_total"]) == Decimal("40")
    assert Decimal(t_sum["reconciled_month_to_date_cost"]) == (
        Decimal(t_sum["month_to_date_cost"]) + Decimal("40")
    )

    # Budget isolation: even though adjustment_total is +40 USD (> 1.00 USD hard limit),
    # operational budget evaluation must NOT count adjustments and must still allow new runs!
    sub2 = client.post(
        "/v1/agent/submit",
        json={**low_risk_payload, "tenant_id": "adj-t"},
    )
    assert sub2.status_code == 200
    assert sub2.json()["status"] == "queued"

    # Governance event recorded
    gov_events = client.get("/v1/governance/events", params={"tenant_id": "adj-t"}).json()
    assert any(e["event_type"] == "finops_cost_adjustment" for e in gov_events)

    # RBAC check when auth_enabled=True
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(
        settings,
        "api_keys",
        "viewer-key:adj-t:viewer,operator-key:adj-t:operator,finops-key:adj-t:finops_admin",
    )
    for denied_key in ("viewer-key", "operator-key"):
        r_denied = client.post(
            "/v1/cost/adjustments",
            headers={"X-API-Key": denied_key},
            json={
                "tenant_id": "adj-t",
                "amount": "1.00",
                "provider_invoice_id": "INV-RBAC-DENY",
                "reason": "RBAC test",
            },
        )
        assert r_denied.status_code == 403

    r_allowed = client.post(
        "/v1/cost/adjustments",
        headers={"X-API-Key": "finops-key"},
        json={
            "tenant_id": "adj-t",
            "amount": "1.00",
            "provider_invoice_id": "INV-RBAC-OK",
            "reason": "RBAC allowed",
        },
    )
    assert r_allowed.status_code == 200


def test_reconciliation_export_json_and_csv_formula_injection_and_rbac(
    client, monkeypatch, low_risk_payload
):
    client.post(
        "/v1/agent/submit",
        json={
            **low_risk_payload,
            "tenant_id": "exp-t",
            "metadata": {"project_id": "proj-exp", "cost_center": "cc-exp"},
        },
    )
    # Adjustment with dangerous formula-like provider_invoice_id to test CSV injection escaping
    client.post(
        "/v1/cost/adjustments",
        json={
            "tenant_id": "exp-t",
            "project_id": "proj-exp",
            "cost_center": "cc-exp",
            "amount": "-0.0001",
            "currency": "USD",
            "provider_invoice_id": "=SUM(A1:A10)",
            "reason": "Formula injection test",
        },
    )

    json_res = client.get(
        "/v1/cost/tenants/exp-t/export",
        params={"format": "json", "project_id": "proj-exp", "cost_center": "cc-exp"},
    )
    assert json_res.status_code == 200
    assert "0E-8" not in json_res.text
    data = json_res.json()
    assert data["tenant_id"] == "exp-t"
    assert data["total_records"] >= 4
    assert data["fx_rates_as_of"] == get_settings().finops_fx_rates_as_of

    csv_res = client.get(
        "/v1/cost/tenants/exp-t/export",
        params={"format": "csv", "settled_only": "true"},
    )
    assert csv_res.status_code == 200
    assert "text/csv" in csv_res.headers["content-type"]
    assert "finops_ledger_exp-t.csv" in csv_res.headers["content-disposition"]
    assert "0E-8" not in csv_res.text

    reader = list(csv.reader(io.StringIO(csv_res.text)))
    header = reader[0]
    assert len(header) == 35
    inv_idx = header.index("provider_invoice_id")
    invoice_cells = [row[inv_idx] for row in reader[1:] if row[inv_idx]]
    assert "'=SUM(A1:A10)" in invoice_cells

    # Truncation check via limit=1
    trunc_res = client.get(
        "/v1/cost/tenants/exp-t/export",
        params={"format": "csv", "limit": 1},
    )
    assert trunc_res.status_code == 200
    assert trunc_res.headers.get("X-Export-Truncated") == "true"

    # RBAC check for export endpoint
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(
        settings,
        "api_keys",
        "viewer-key:exp-t:viewer,operator-key:exp-t:operator,tenant-admin-key:exp-t:tenant_admin,other-tenant-admin:other-t:tenant_admin",
    )
    assert (
        client.get("/v1/cost/tenants/exp-t/export", headers={"X-API-Key": "viewer-key"}).status_code
        == 403
    )
    assert (
        client.get("/v1/cost/tenants/exp-t/export", headers={"X-API-Key": "operator-key"}).status_code
        == 403
    )
    assert (
        client.get("/v1/cost/tenants/exp-t/export", headers={"X-API-Key": "other-tenant-admin"}).status_code
        == 403
    )
    assert (
        client.get("/v1/cost/tenants/exp-t/export", headers={"X-API-Key": "tenant-admin-key"}).status_code
        == 200
    )


def test_dashboard_v155_views_and_metrics(client):
    # 1. Playground with cost_center and org_id
    pg_res = client.post(
        "/dashboard/playground",
        data={
            "tenant_id": "ui-v155-t",
            "user_id": "ui-tester",
            "task_type": "analysis",
            "allowed_tools": "read_repo",
            "max_tool_calls": "4",
            "provider": "internal",
            "model_name": "deterministic-control-plane",
            "pricing_mode": "standard",
            "project_id": "proj-ui",
            "cost_center": "cc-ui-team",
            "org_id": "org-tw",
            "goal": "Verify v1.5.5 UI",
            "prompt": "Check cost center and FX rendering.",
            "no_external_network": "on",
        },
    )
    assert pg_res.status_code == 200
    assert "0E-8" not in pg_res.text

    runs = client.get("/v1/runs", params={"tenant_id": "ui-v155-t"}).json()
    run_id = runs[0]["run_id"]

    # 2. Run detail view
    rd_res = client.get(f"/dashboard/runs/{run_id}")
    assert rd_res.status_code == 200
    assert "FinOps ledger" in rd_res.text
    assert "cc-ui-team" in rd_res.text
    assert "0E-8" not in rd_res.text

    # 3. Dashboard costs adjustment form + costs view + export routes
    adj_res = client.post(
        "/dashboard/costs/adjustments",
        data={
            "tenant_id": "ui-v155-t",
            "project_id": "proj-ui",
            "cost_center": "cc-ui-team",
            "provider": "internal",
            "amount": "-0.0002",
            "currency": "USD",
            "provider_invoice_id": "INV-UI-01",
            "reason": "UI credit test",
        },
        follow_redirects=True,
    )
    assert adj_res.status_code == 200
    assert "Agent FinOps Core" in adj_res.text
    assert "cc-ui-team" in adj_res.text
    assert "0E-8" not in adj_res.text

    ui_csv = client.get("/dashboard/costs/export", params={"tenant_id": "ui-v155-t", "format": "csv"})
    assert ui_csv.status_code == 200
    assert "text/csv" in ui_csv.headers["content-type"]

    ui_json = client.get("/dashboard/costs/export", params={"tenant_id": "ui-v155-t", "format": "json"})
    assert ui_json.status_code == 200
    assert ui_json.json()["tenant_id"] == "ui-v155-t"

    # 4. Pricing detail edit tax_rate / fx_rate / tax_included
    p_create = client.post(
        "/v1/pricing",
        json={
            "provider": "qa_local",
            "model_name": "ui-fx-model",
            "resource_type": "model",
            "resource_subtype": "chat_completion",
            "price_unit": "per_call",
            "unit_price": "10",
            "currency": "TWD",
        },
    ).json()
    pid = p_create["pricing_id"]
    p_edit = client.post(
        f"/dashboard/admin/finops/pricing/{pid}/edit",
        data={
            "unit_price": "10",
            "currency": "TWD",
            "tax_rate": "0.05",
            "fx_rate": "0.03125",
            "tax_included": "on",
            "is_active": "on",
            "effective_from": p_create["effective_from"],
            "change_reason": "Set TWD tax and FX",
        },
        follow_redirects=True,
    )
    assert p_edit.status_code == 200
    assert "0.05" in p_edit.text
    assert "0.03125" in p_edit.text

    # 5. Prometheus metrics check
    metrics_res = client.get("/v1/metrics")
    assert metrics_res.status_code == 200
    assert "agent_finops_base_currency_cost_total" in metrics_res.text
    assert "agent_finops_tax_total" in metrics_res.text
    assert "agent_finops_ledger_exports_total" in metrics_res.text
