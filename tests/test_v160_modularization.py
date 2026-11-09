"""v1.6.0 P1: FinOps package split, FinOps router move, leaf modules and architecture contracts (D25-D28)."""

from __future__ import annotations

import importlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.openapi_contract import current_contract

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "tests" / "fixtures" / "v160" / "openapi_v1512.json"

# Every name imported from ``app.services.finops`` anywhere in app/, tests/ or scripts/ before the split.
FORMER_EXTERNAL_NAMES = [
    "CSV_EXPORT_COLUMNS", "FINOPS_CALCULATION_VERSION", "acknowledge_budget_violation", "acquire_run_budget_lease",
    "budget_to_dict", "compute_tenant_burn_rate_forecast", "cost_to_dict", "create_budget_policy",
    "create_cost_adjustment", "create_pricing_item", "disable_budget_policy", "disable_pricing_item",
    "ensure_default_pricing", "export_tenant_cost_ledger", "extract_allocation", "format_decimal_plain",
    "get_budget_policy", "get_budget_violation", "get_pricing_item", "list_budget_policies",
    "list_budget_violations", "list_pricing_items", "list_tenant_token_leases", "list_usage_events",
    "model_cost_ranking", "pricing_to_dict", "record_actual_usage", "record_checkpoint_usage",
    "record_replay_usage", "record_submit_usage", "record_usage_event", "release_run_budget_lease",
    "render_cost_ledger_csv", "resolve_budget_violation", "run_cost_summary", "tenant_cost_summary",
    "update_budget_policy", "update_pricing_item", "usage_to_dict", "violation_to_dict", "workflow_cost_summary",
]

SUBMODULES = [
    "core", "settlement", "serializers", "pricing", "ledger", "budget_policies", "forecast", "leases",
    "budget_enforcement", "recording", "reporting", "adjustments", "export",
]

FINOPS_PATH_PREFIXES = ("/v1/pricing", "/v1/admin/finops", "/v1/usage-events", "/v1/cost/", "/v1/budgets", "/v1/budget-violations")


def test_former_single_module_is_gone_and_package_exists():
    assert not (ROOT / "app" / "services" / "finops.py").exists()
    package = ROOT / "app" / "services" / "finops"
    assert (package / "__init__.py").exists()
    for name in SUBMODULES:
        assert (package / f"{name}.py").exists(), name


def test_facade_re_exports_every_former_name():
    finops = importlib.import_module("app.services.finops")
    missing = [name for name in FORMER_EXTERNAL_NAMES if not hasattr(finops, name)]
    assert missing == []
    assert set(FORMER_EXTERNAL_NAMES) <= set(finops.__all__)


def test_facade_exports_every_public_submodule_name():
    finops = importlib.import_module("app.services.finops")
    for sub in SUBMODULES:
        module = importlib.import_module(f"app.services.finops.{sub}")
        for name, value in vars(module).items():
            if name.startswith("_") or getattr(value, "__module__", None) != module.__name__:
                continue
            assert getattr(finops, name) is value, f"{sub}.{name}"


def test_calculation_version_lives_in_core():
    from app.services import finops
    from app.services.finops import core

    assert finops.FINOPS_CALCULATION_VERSION is core.FINOPS_CALCULATION_VERSION
    assert core.FINOPS_CALCULATION_VERSION.startswith("v1.")


def test_openapi_contract_matches_v1512_baseline():
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    current = current_contract()
    missing = sorted(key for key in baseline if key not in current)
    changed = sorted(key for key in baseline if key in current and baseline[key] != current[key])
    assert missing == [], f"operations removed: {missing}"
    assert changed == [], f"operations changed: {changed}"


def test_finops_paths_are_served_by_the_finops_router():
    from app.main import app

    owners: dict[str, set[str]] = {}
    for route in app.routes:
        path = getattr(route, "path", "")
        if path.startswith(FINOPS_PATH_PREFIXES):
            owners.setdefault(route.endpoint.__module__, set()).add(path)
    assert set(owners) == {"app.api.routers.finops"}, owners
    assert len(owners["app.api.routers.finops"]) >= 30


def test_leaf_modules_are_re_exported_unchanged():
    from app.services import audit_log, governance_audit, mcp_gateway, network_policy, sensitive_data

    assert mcp_gateway.is_external_endpoint is network_policy.is_external_endpoint
    assert mcp_gateway.SENSITIVE_KEY_NAMES is sensitive_data.SENSITIVE_KEY_NAMES
    assert mcp_gateway.SENSITIVE_KEY_SUFFIXES is sensitive_data.SENSITIVE_KEY_SUFFIXES
    assert mcp_gateway.SENSITIVE_VALUE_PATTERNS is sensitive_data.SENSITIVE_VALUE_PATTERNS
    assert audit_log.persist_governance_event is governance_audit.persist_governance_event


@pytest.mark.parametrize(
    "path",
    [
        "app/services/sensitive_data.py",
        "app/services/network_policy.py",
    ],
)
def test_leaf_modules_import_nothing_from_the_app(path):
    source = (ROOT / path).read_text(encoding="utf-8")
    assert "from app." not in source and "import app" not in source


def test_architecture_contracts_pass():
    executable = shutil.which("lint-imports")
    if executable is None:
        pytest.skip("import-linter is not installed (pip install -r requirements.txt)")
    result = subprocess.run([executable], cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
    assert "0 broken" in result.stdout
