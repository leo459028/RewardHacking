"""v1.6.3 P1b (plan D19): every "is this production?" decision uses one rule, every APP_ENV other than dev / test.

Before D19 these checks compared ``APP_ENV`` with the exact value ``prod``, so ``production``, ``staging`` or ``uat``
silently got the development behaviour. Each check has positive cases (production-like values) and negative cases
(dev / test keep the advisory or relaxed behaviour). The FinOps ledger ``environment`` label and the Secure cookie rule
are deliberately not part of D19.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from pathlib import Path

import pytest

from app.core.config import Settings
from app.models.finops import BudgetViolation
from app.models.judge import JudgeAssessment
from app.services import benchmark, operations
from app.services.evaluation import metrics as m
from app.services.sandbox import docker_config

PRODUCTION_LIKE = ["prod", "production", "staging", "uat", " PROD "]
DEVELOPMENT = ["dev", "test"]


def _settings(app_env: str, **values) -> Settings:
    return Settings(_env_file=None, app_env=app_env, **values)


def _db(session_factory, fn):
    async def scenario():
        async with session_factory() as session:
            result = await fn(session)
            await session.commit()
            return result

    return asyncio.run(scenario())


def _readiness_checks(session_factory, settings: Settings) -> dict[str, operations.OperationsCheck]:
    readiness = _db(session_factory, lambda s: operations.compute_operations_readiness(s, settings=settings))
    return {check.check_key: check for check in readiness.checks}


# ---------------------------------------------------------------------------
# The rule itself is used everywhere
# ---------------------------------------------------------------------------


def test_no_code_compares_app_env_with_the_literal_prod():
    offenders = []
    for path in Path("app").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if re.search(r"""==\s*["']prod["']""", text):
            offenders.append(str(path).replace("\\", "/"))
    assert offenders == []


# ---------------------------------------------------------------------------
# Dashboard sign-in, audit chain, dual control (Operations checks without data)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("app_env", PRODUCTION_LIKE)
def test_dashboard_off_fails_in_every_production_like_env(session_factory, app_env):
    check = _db(session_factory, lambda s: operations._dashboard_auth_mode_check(s, settings=_settings(app_env, dashboard_auth_mode="off")))
    assert check.status == "fail" and check.metadata["problems"] == ["auth_off_in_prod"]


@pytest.mark.parametrize("app_env", DEVELOPMENT)
def test_dashboard_off_passes_in_dev_and_test(session_factory, app_env):
    check = _db(session_factory, lambda s: operations._dashboard_auth_mode_check(s, settings=_settings(app_env, dashboard_auth_mode="off")))
    assert check.status == "pass" and check.metadata["problems"] == []


@pytest.mark.parametrize(("app_env", "advisory"), [(env, False) for env in PRODUCTION_LIKE] + [(env, True) for env in DEVELOPMENT])
def test_audit_chain_without_signing_key_is_advisory_only_in_dev_and_test(session_factory, app_env, advisory):
    settings = _settings(app_env, audit_chain_signing_key_file="")
    check = _db(session_factory, lambda s: operations._audit_chain_integrity_check(s, settings=settings))
    assert "no_signing_key" in check.metadata["problems"]
    assert check.metadata["advisory"] is advisory
    assert check.status == ("warn" if advisory else "fail")


@pytest.mark.parametrize(("app_env", "advisory"), [(env, False) for env in PRODUCTION_LIKE] + [(env, True) for env in DEVELOPMENT])
def test_missing_dual_control_is_advisory_only_in_dev_and_test(app_env, advisory):
    check = operations._dual_control_configured_check(_settings(app_env, dual_control_actions=""))
    assert check.status == "warn" and check.metadata["advisory"] is advisory


@pytest.mark.parametrize("app_env", PRODUCTION_LIKE + DEVELOPMENT)
def test_configured_dual_control_passes_everywhere(app_env):
    check = operations._dual_control_configured_check(_settings(app_env, dual_control_actions="retention.apply,legal_hold.release"))
    assert check.status == "pass"


# ---------------------------------------------------------------------------
# Safety regression evidence and critical FinOps violations (full readiness)
# ---------------------------------------------------------------------------


def _open_critical_violation(session):
    session.add(BudgetViolation(violation_id=f"bv_{uuid.uuid4().hex[:12]}", violation_type="hard_limit", severity="critical"))

    async def done():
        return None

    return done()


@pytest.mark.parametrize(("app_env", "status"), [("staging", "fail"), ("production", "fail"), ("prod", "fail"), ("dev", "warn"), ("test", "warn")])
def test_missing_safety_evidence_and_critical_violations_follow_the_rule(session_factory, app_env, status):
    _db(session_factory, _open_critical_violation)
    checks = _readiness_checks(session_factory, _settings(app_env))
    assert checks["safety_regression_evidence"].status == status
    assert checks["finops_open_critical_violations"].status == status


def test_no_critical_violation_passes_in_production(session_factory):
    checks = _readiness_checks(session_factory, _settings("staging"))
    assert checks["finops_open_critical_violations"].status == "pass"


# ---------------------------------------------------------------------------
# Judge same-family warning
# ---------------------------------------------------------------------------


def _same_family_assessment(session):
    session.add(JudgeAssessment(assessment_id=f"jdg_{uuid.uuid4().hex[:12]}", same_family_warning=True))

    async def done():
        return None

    return done()


def _judge_check(session_factory, app_env: str):
    from datetime import datetime, timezone

    settings = _settings(app_env)
    return _db(session_factory, lambda s: operations._evaluation_judge_health_check(s, settings=settings, now=datetime.now(timezone.utc)))


@pytest.mark.parametrize("app_env", ["staging", "production", "prod"])
def test_same_family_judge_warns_in_every_production_like_env(session_factory, app_env):
    _db(session_factory, _same_family_assessment)
    check = _judge_check(session_factory, app_env)
    assert check.metadata["same_family_warnings_24h"] == 1
    assert check.status == "warn" and "model family" in check.detail


@pytest.mark.parametrize("app_env", DEVELOPMENT)
def test_same_family_judge_is_not_flagged_in_dev_and_test(session_factory, app_env):
    _db(session_factory, _same_family_assessment)
    check = _judge_check(session_factory, app_env)
    assert check.metadata["same_family_warnings_24h"] == 1  # counted, but not flagged
    assert "model family" not in check.detail


# ---------------------------------------------------------------------------
# Benchmark gate (reward-tampering TPR below the threshold)
# ---------------------------------------------------------------------------


def _low_tpr_metrics(settings: Settings) -> dict:
    cell = {"tpr": 0.5, "positives": 20, "insufficient_samples": False}
    return {"as_configured": {"by_kind": {"reward_tampering": {"tpr_at_fpr": {m.fpr_key(settings.evaluation_benchmark_gate_fpr): cell}}}}}


@pytest.mark.parametrize(("app_env", "status"), [(env, "fail") for env in PRODUCTION_LIKE] + [(env, "warn") for env in DEVELOPMENT])
def test_benchmark_gate_low_tpr_follows_the_rule(app_env, status):
    settings = _settings(app_env)
    assert benchmark.gate_evaluation(_low_tpr_metrics(settings), settings) == (status, "reward_tampering_tpr_below_threshold")


# ---------------------------------------------------------------------------
# Sandbox: local image IDs (a runtime behaviour, not only a check)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("app_env", PRODUCTION_LIKE + [""])
def test_local_image_ids_are_refused_in_every_production_like_env(app_env):
    assert docker_config.local_image_ids_allowed(_settings(app_env, sandbox_docker_allow_local_image_ids=True)) is False


@pytest.mark.parametrize("app_env", DEVELOPMENT)
def test_local_image_ids_still_work_in_dev_and_test(app_env):
    assert docker_config.local_image_ids_allowed(_settings(app_env, sandbox_docker_allow_local_image_ids=True)) is True
    assert docker_config.local_image_ids_allowed(_settings(app_env, sandbox_docker_allow_local_image_ids=False)) is False


def test_local_image_ids_without_any_app_env_attribute_are_refused():
    class Bare:
        sandbox_docker_allow_local_image_ids = True

    assert docker_config.local_image_ids_allowed(Bare()) is False


# ---------------------------------------------------------------------------
# Deliberately out of D19
# ---------------------------------------------------------------------------


def test_cookie_secure_rule_is_unchanged():
    # Stricter than D19: Secure everywhere except dev, including test.
    assert _settings("dev").dashboard_cookie_secure_effective is False
    assert _settings("test").dashboard_cookie_secure_effective is True
    assert _settings("staging").dashboard_cookie_secure_effective is True
