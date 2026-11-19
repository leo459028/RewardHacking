"""v1.6.2 P2a: sandbox migration, models, settings, hardening case list, backend registry and isolation policy."""

from __future__ import annotations

import importlib.util
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.core.config import Settings
from app.models import sandbox as models
from app.models.base import Base
from app.services import sandbox
from app.services.sandbox import IsolationLevel, SimulatedBackend, conformance, docker_config, health, policy
from app.services.sandbox.spec import ExecutionSpec, Limits, image_reference_valid, validate_spec

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "versions" / "202611180001_add_v162_sandbox_verification.py"
TABLES = {"sandbox_jobs", "sandbox_conformance_runs", "verification_suites", "sandbox_artifacts", "verification_runs"}
PINNED = "ghcr.io/acme/python@sha256:" + "a" * 64
IMAGE_ID = "sha256:" + "b" * 64


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def _docker_settings(**overrides) -> Settings:
    return _settings(sandbox_backend="docker", **overrides)


def _load_migration():
    spec = importlib.util.spec_from_file_location("v162_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _apply(connection, function) -> None:
    context = MigrationContext.configure(connection)
    with Operations.context(context):
        function()


# ---------------------------------------------------------------------------
# D42: migration and models
# ---------------------------------------------------------------------------


def test_migration_follows_the_head_with_the_real_creation_date():
    module = _load_migration()
    assert (module.revision, module.down_revision) == ("202611180001", "202611120003")
    assert "Create Date: 2026-11-18" in module.__doc__


def test_migration_creates_exactly_the_model_columns_and_downgrades_cleanly():
    module = _load_migration()
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        _apply(connection, module.upgrade)
        inspector = sa.inspect(connection)
        assert TABLES <= set(inspector.get_table_names())
        for table in TABLES:
            migrated = {column["name"] for column in inspector.get_columns(table)}
            assert migrated == set(Base.metadata.tables[table].columns.keys()), table
            indexed = {index["name"] for index in inspector.get_indexes(table)}
            modelled = {index.name for index in Base.metadata.tables[table].indexes}
            assert indexed == modelled, table
        _apply(connection, module.downgrade)
        assert not TABLES & set(sa.inspect(connection).get_table_names())
    engine.dispose()


def test_migrated_defaults_allow_a_minimal_job_row():
    module = _load_migration()
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        _apply(connection, module.upgrade)
        connection.execute(sa.text("INSERT INTO sandbox_jobs (job_id, kind) VALUES ('sbj_1', 'conformance')"))
        row = connection.execute(sa.text("SELECT status, attempts, error_code, tenant_id FROM sandbox_jobs")).one()
        assert tuple(row) == ("queued", 0, "", "")
    engine.dispose()


def test_models_are_registered_for_autogenerate():
    text = (ROOT / "migrations" / "env.py").read_text(encoding="utf-8")
    for name in ("SandboxJob", "SandboxConformanceRun", "VerificationSuite", "SandboxArtifact", "VerificationRun"):
        assert name in text and hasattr(models, name)


# ---------------------------------------------------------------------------
# D43: settings keep the previous behaviour by default
# ---------------------------------------------------------------------------


def test_settings_defaults_keep_the_sandbox_off():
    settings = _settings()
    assert settings.sandbox_backend == "simulated" and settings.sandbox_execution_enabled is False
    assert settings.sandbox_docker_runtime == "runc" and settings.sandbox_docker_user == "65534:65534"
    assert settings.sandbox_docker_pull == "never" and settings.sandbox_docker_allow_local_image_ids is False
    assert settings.sandbox_conformance_max_age_hours == 168 and settings.sandbox_images == {}
    assert settings.sandbox_artifact_max_bytes == 20 * 1024 * 1024
    assert settings.sandbox_verify_mode == "off" and settings.sandbox_verify_daily_cap_per_tenant == 50
    assert settings.sandbox_verify_flaky_retries == 2 and settings.sandbox_compute_cost_per_cpu_second == Decimal("0")
    assert image_reference_valid(settings.sandbox_conformance_image)


def test_empty_sandbox_images_means_no_images(monkeypatch):
    monkeypatch.setenv("SANDBOX_IMAGES", "")
    assert Settings(_env_file=None).sandbox_images == {}
    monkeypatch.setenv("SANDBOX_IMAGES", '{"python": "%s"}' % PINNED)
    assert Settings(_env_file=None).sandbox_images == {"python": PINNED}


# ---------------------------------------------------------------------------
# D15: conformance lists
# ---------------------------------------------------------------------------


def test_hardening_case_names_are_the_documented_list():
    assert conformance.HARDENING_CASE_NAMES == (
        "root_fs_read_only",
        "network_unreachable",
        "runs_as_non_root",
        "capabilities_dropped",
        "no_new_privileges",
        "pids_limit_enforced",
        "memory_limit_enforced",
        "host_env_not_visible",
        "workspace_isolated",
        "runtime_matches_config",
    )
    assert conformance.ALL_CASE_NAMES == conformance.CASE_NAMES + conformance.HARDENING_CASE_NAMES


def test_base_case_list_is_unchanged():
    assert len(conformance.CASES) == 10 and conformance.CASES[0].__name__ == "rejects_invalid_spec"


def test_simulated_backend_is_not_applicable_for_hardening():
    results = conformance.run_hardening_cases(SimulatedBackend)
    assert [item.name for item in results] == list(conformance.HARDENING_CASE_NAMES)
    assert all(item.not_applicable and not item.passed for item in results)
    assert results[0].to_dict()["not_applicable"] is True
    assert "not_applicable" not in conformance.CaseResult("x", True).to_dict()


def test_suite_passed_needs_every_case_to_pass():
    report = [{"name": name, "passed": True} for name in conformance.ALL_CASE_NAMES]
    assert conformance.suite_passed(report)
    assert not conformance.suite_passed(report[:-1])
    assert not conformance.suite_passed([{**report[0], "passed": False}, *report[1:]])
    assert not conformance.suite_passed([])


def test_simulated_backend_never_passes_the_full_suite():
    assert not conformance.suite_passed(conformance.run_full_suite(SimulatedBackend, image=conformance.CONFORMANCE_IMAGE))


# ---------------------------------------------------------------------------
# D19 / D23: backend registry
# ---------------------------------------------------------------------------


def test_docker_backend_is_unavailable_outside_the_runner():
    with pytest.raises(sandbox.SandboxConfigError, match="sandbox-runner"):
        sandbox.get_backend(_docker_settings())


def test_agent_run_tools_stay_simulated_with_the_docker_backend():
    backend = sandbox.get_tool_backend(_docker_settings())
    assert backend.capabilities().isolation_level == IsolationLevel.SIMULATED


def test_unknown_backend_fails_closed_for_tools_too():
    with pytest.raises(sandbox.SandboxConfigError, match="Unsupported"):
        sandbox.get_tool_backend(_settings(sandbox_backend="kubernetes"))


# ---------------------------------------------------------------------------
# D18 / D20: images, capabilities and isolation enforcement
# ---------------------------------------------------------------------------


def test_image_ids_need_the_explicit_setting():
    caps = docker_config.capabilities(_docker_settings())
    spec = ExecutionSpec("t", ("true",), Limits(5, 500, 64, 16, 1024, 1024), image=IMAGE_ID)
    assert validate_spec(spec, caps) == ["image_not_digest_pinned"]
    assert validate_spec(spec, caps, allow_image_ids=True) == []
    assert validate_spec(replace(spec, image="sha256:abc"), caps, allow_image_ids=True) == ["image_not_digest_pinned"]


def test_production_never_allows_image_ids():
    assert docker_config.local_image_ids_allowed(_docker_settings(sandbox_docker_allow_local_image_ids=True))
    assert not docker_config.local_image_ids_allowed(_docker_settings(sandbox_docker_allow_local_image_ids=True, app_env="prod"))
    assert not docker_config.local_image_ids_allowed(_docker_settings())


@pytest.mark.parametrize(("runtime", "level"), [("runc", IsolationLevel.CONTAINER), ("runsc", IsolationLevel.GVISOR)])
def test_runtime_decides_the_isolation_level(runtime, level):
    caps = docker_config.capabilities(_docker_settings(sandbox_docker_runtime=runtime))
    assert caps.isolation_level == level and caps.backend_id == "docker" and caps.supports_network == frozenset({"none"})


@pytest.mark.parametrize(
    ("overrides", "problem"),
    [
        ({"sandbox_docker_runtime": "kata"}, "runtime_invalid"),
        ({"sandbox_docker_user": "0:0"}, "docker_user_invalid"),
        ({"sandbox_docker_user": "root"}, "docker_user_invalid"),
        ({"sandbox_docker_user": "1000:0"}, "docker_user_invalid"),
        ({"sandbox_docker_pull": "always"}, "pull_policy_invalid"),
        ({"sandbox_max_timeout_seconds": 0}, "limits_invalid"),
        ({"sandbox_conformance_image": "busybox:latest"}, "conformance_image_invalid"),
        ({"sandbox_images": {"python": "python:3.12"}}, "sandbox_image_invalid"),
        ({"sandbox_images": {"python": IMAGE_ID}}, "sandbox_image_invalid"),
    ],
)
def test_invalid_docker_settings_are_reported(overrides, problem):
    assert problem in docker_config.config_problems(_docker_settings(**overrides))


def test_valid_docker_settings_have_no_problems():
    settings = _docker_settings(sandbox_images={"python": PINNED})
    assert docker_config.config_problems(settings) == []
    local = _docker_settings(sandbox_images={"python": IMAGE_ID}, sandbox_docker_allow_local_image_ids=True)
    assert docker_config.config_problems(local) == []


def test_unknown_runtime_has_no_capabilities():
    with pytest.raises(sandbox.SandboxConfigError, match="SANDBOX_DOCKER_RUNTIME"):
        docker_config.capabilities(_docker_settings(sandbox_docker_runtime="kata"))


def test_isolation_is_enforced_for_work_that_executes():
    settings = _docker_settings()
    assert policy.isolation_rejection("container", 0.1, settings) is None
    assert policy.isolation_rejection("container", 0.9, settings) == "isolation_insufficient"
    assert policy.isolation_rejection("container", None, settings) == "isolation_insufficient"
    assert policy.isolation_rejection("gvisor", 0.9, settings) is None


def test_simulated_level_is_never_refused_by_the_isolation_policy():
    assert policy.isolation_rejection("simulated", 0.9, _settings()) is None


def test_lowering_the_high_risk_minimum_is_visible():
    assert not policy.high_risk_relaxed(_settings())
    relaxed = _settings(sandbox_min_isolation_high_risk="container")
    assert policy.high_risk_relaxed(relaxed) and policy.isolation_rejection("container", 0.9, relaxed) is None


# ---------------------------------------------------------------------------
# D19: the status report never touches Docker
# ---------------------------------------------------------------------------


def test_status_report_for_docker_reads_settings_only():
    report = health.status_report(_docker_settings())
    assert report["status"] == "pass" and report["executes_tools"] is False and report["error"] is None
    assert report["capabilities"]["backend_id"] == "docker" and report["isolation_policy"]["mode"] == "enforced"
    assert report["smoke"]["cases"] == []


def test_status_report_for_invalid_docker_settings_fails():
    report = health.status_report(_docker_settings(sandbox_docker_user="0:0"))
    assert report["status"] == "fail" and "docker_user_invalid" in report["error"] and report["capabilities"] is None


def test_status_report_for_simulated_is_unchanged():
    report = health.status_report(_settings())
    assert report["status"] == "pass" and report["isolation_policy"]["mode"] == "record_only"
    assert report["capabilities"]["backend_id"] == "simulated" and report["smoke"]["passed"] is True
