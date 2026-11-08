"""v1.5.11 Real Local & Cloud Judges: judge profiles, backend compatibility and background triage."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.models.evaluation_review import EvaluationReview, TenantEvaluationPolicy
from app.models.judge import JudgeAssessment
from app.services.evaluation import judges as judges_pkg
from app.services.evaluation.judges import DeterministicJudge, OpenAICompatibleJudge, get_judge
from app.services.evaluation.judges import profiles as jp

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = ROOT / "migrations" / "versions"
EXAMPLE_PROFILES = ROOT / "config" / "judge_profiles.example.yaml"


def _load_migration(filename: str):
    spec = importlib.util.spec_from_file_location(filename.removesuffix(".py"), MIGRATIONS_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _settings(**values) -> Settings:
    # An empty file path keeps a developer's local config/judge_profiles.yaml out of the tests.
    values.setdefault("evaluation_judge_profiles_file", "")
    return Settings(**values)


def _profile(**overrides) -> dict:
    data = {
        "provider": "openai_compatible",
        "base_url": "http://localhost:1234/v1",
        "model": "qwen3.5-2b",
        "timeout_seconds": 30,
    }
    data.update(overrides)
    return data


# ---------------------------------------------------------------------------
# P1: migration, models, settings and judge profiles
# ---------------------------------------------------------------------------


def test_v1511_migration_is_single_head_after_v1510():
    module = _load_migration("202611100001_add_v1511_judge_profiles.py")
    assert module.revision == "202611100001"
    assert module.down_revision == "202611090001"
    revisions: dict[str, str | None] = {}
    for path in MIGRATIONS_DIR.glob("*.py"):
        mod = _load_migration(path.name)
        revisions[mod.revision] = mod.down_revision
    referenced = {down for down in revisions.values() if down}
    assert sorted(set(revisions) - referenced) == ["202611100001"]


def test_v1511_columns_and_defaults(session_factory):
    async def scenario():
        async with session_factory() as session:
            def _describe(sync_session):
                inspector = sa_inspect(sync_session.connection())
                return {
                    "assessments": {c["name"] for c in inspector.get_columns("judge_assessments")},
                    "policies": {c["name"] for c in inspector.get_columns("tenant_evaluation_policies")},
                }

            described = await session.run_sync(_describe)
            session.add(JudgeAssessment(assessment_id="jdg_p1", run_id="run_p1"))
            session.add(TenantEvaluationPolicy(tenant_id="t-p1"))
            await session.commit()
            assessment = (await session.execute(select(JudgeAssessment))).scalar_one()
            policy = (await session.execute(select(TenantEvaluationPolicy))).scalar_one()
            return described, assessment, policy

    described, assessment, policy = asyncio.run(scenario())
    assert "profile" in described["assessments"]
    assert {"allow_external_judge", "external_judge_profiles"} <= described["policies"]
    assert assessment.profile == ""
    assert policy.allow_external_judge is False and policy.external_judge_profiles == []


def test_v1511_settings_defaults_and_validation():
    settings = _settings()
    assert settings.evaluation_judge_profile == "default"
    assert settings.evaluation_judge_internal_hosts == "host.docker.internal"
    assert settings.judge_internal_host_list == ["host.docker.internal"]
    assert settings.evaluation_judge_background_concurrency == 1
    assert settings.evaluation_judge_pending_sweep_seconds == 600
    assert Settings(evaluation_judge_profile="  Local-Small ").evaluation_judge_profile == "local-small"
    with pytest.raises(ValueError):
        Settings(evaluation_judge_background_concurrency=0)


def test_without_profiles_default_matches_v1510_behaviour():
    settings = _settings(
        evaluation_judge_provider="openai_compatible", evaluation_judge_base_url="http://localhost:8001/v1", evaluation_judge_model="m-1"
    )
    profiles = jp.load_profiles(settings)
    assert set(profiles) == {"deterministic", "default"}
    default = profiles["default"]
    assert default.legacy is True and default.structured_output == "json_object" and default.execution == "inline"
    assert default.timeout_seconds == settings.evaluation_judge_timeout_seconds and default.max_retries == 0
    judge = get_judge(settings)
    assert isinstance(judge, OpenAICompatibleJudge)
    # v1.5.10 version strings are kept for the synthesized default (dedup and benchmark continuity).
    assert judge.version().startswith("openai_compatible:m-1:")
    assert judge.profile_label == "default" and judge.structured_output == "json_object"
    # The plain deterministic default is unchanged too.
    assert isinstance(get_judge(_settings()), DeterministicJudge)
    assert get_judge(_settings()).version() == "deterministic:1.0.0"


def test_example_profiles_file_loads_and_prefixes_judge_version():
    settings = _settings(evaluation_judge_profiles_file=str(EXAMPLE_PROFILES))
    profiles = jp.load_profiles(settings)
    assert {"deterministic", "default", "local-small", "local-large", "gemini-flash"} <= set(profiles)
    small = profiles["local-small"]
    assert (small.execution, small.structured_output, small.pricing_provider, small.max_input_chars) == ("background", "auto", "qa_local", 8000)
    gemini = profiles["gemini-flash"]
    assert gemini.external == "true" and gemini.api_key_env == "GEMINI_API_KEY" and gemini.pricing_provider == "google"
    judge = get_judge(settings, profile="local-small")
    assert judge.version().startswith("local-small:openai_compatible:qwen3.5-2b:")
    assert judge.profile_label == "local-small"
    with pytest.raises(jp.JudgeProfileError):
        get_judge(settings, profile="no-such-profile")


def test_env_json_overrides_file_profile(tmp_path):
    profile_file = tmp_path / "profiles.yaml"
    profile_file.write_text("profiles:\n  - name: lab\n    provider: openai_compatible\n    base_url: http://localhost:1234/v1\n    model: a\n", encoding="utf-8")
    settings = Settings(evaluation_judge_profiles_file=str(profile_file), evaluation_judge_profiles={"lab": _profile(model="b")})
    lab = jp.load_profiles(settings)["lab"]
    assert lab.model == "b" and lab.source == "env"
    # A mapping form is accepted as well, and "default" may be redefined (no longer legacy).
    mapped = _settings(evaluation_judge_profiles={"default": _profile(model="c")})
    assert jp.load_profiles(mapped)["default"].legacy is False


@pytest.mark.parametrize(
    "raw, message",
    [
        (_profile(api_key="sk-test_not_a_real_value"), "api_key_env"),
        (_profile(token="x"), "secret field"),
        (_profile(provider="magic"), "provider"),
        (_profile(structured_output="xml"), "structured_output"),
        (_profile(execution="later"), "execution"),
        (_profile(pricing_provider="acme"), "pricing_provider"),
        (_profile(api_key_env="lower-case"), "api_key_env"),
        (_profile(timeout_seconds=0), "timeout_seconds"),
        (_profile(max_retries=9), "max_retries"),
        (_profile(unexpected=True), "unknown field"),
        (_profile(base_url=""), "base_url and model"),
    ],
)
def test_invalid_profiles_are_rejected(raw, message):
    with pytest.raises(jp.JudgeProfileError) as excinfo:
        jp.load_profiles(_settings(evaluation_judge_profiles={"lab": raw}))
    assert message in str(excinfo.value)


def test_reserved_and_malformed_names_are_rejected():
    for name in ("deterministic", "Bad_Name", "x"):
        with pytest.raises(jp.JudgeProfileError):
            jp.load_profiles(_settings(evaluation_judge_profiles={name: _profile()}))


def test_invalid_profile_file_fails_startup(tmp_path, monkeypatch):
    profile_file = tmp_path / "judge_profiles.yaml"
    profile_file.write_text("profiles:\n  - name: leaky\n    provider: openai_compatible\n    api_key: sk-test_not_a_real_value\n", encoding="utf-8")
    monkeypatch.setenv("EVALUATION_JUDGE_PROFILES_FILE", str(profile_file))
    get_settings.cache_clear()
    from fastapi.testclient import TestClient

    from app.main import app

    with pytest.raises(jp.JudgeProfileError):
        with TestClient(app):
            pass
    get_settings.cache_clear()


def test_unknown_active_profile_falls_back_and_health_fails(session_factory, monkeypatch):
    monkeypatch.setenv("EVALUATION_JUDGE_PROFILE", "missing-profile")
    monkeypatch.setenv("EVALUATION_JUDGE_PROFILES_FILE", "")
    get_settings.cache_clear()
    settings = get_settings()
    assert jp.active_profile_known(settings) is False
    assert isinstance(get_judge(settings), DeterministicJudge)

    from app.services.operations import compute_operations_readiness

    async def scenario():
        async with session_factory() as session:
            return await compute_operations_readiness(session, settings=settings)

    checks = {check.check_key: check for check in asyncio.run(scenario()).checks}
    health = checks["evaluation_judge_health"]
    assert health.status == "fail" and health.metadata["profile_known"] is False and health.metadata["advisory"] is True
    get_settings.cache_clear()


def test_api_key_is_read_from_env_and_never_exposed(monkeypatch):
    monkeypatch.setenv("LAB_JUDGE_KEY", "lab-key-placeholder-value")
    settings = _settings(evaluation_judge_profiles={"lab": _profile(api_key_env="LAB_JUDGE_KEY")})
    lab = jp.get_profile(settings, "lab")
    assert lab.api_key(settings) == "lab-key-placeholder-value"
    public = lab.public_dict(settings)
    assert public["api_key_configured"] is True and public["host"] == "localhost"
    assert "lab-key-placeholder-value" not in repr(lab) and "lab-key-placeholder-value" not in json.dumps(public)
    monkeypatch.delenv("LAB_JUDGE_KEY")
    assert lab.api_key_configured(settings) is False


def _chat_response(content: dict) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(content)}}], "usage": {"prompt_tokens": 50, "completion_tokens": 10}})


def test_two_profiles_assess_the_same_review_separately(client, session_factory, monkeypatch):
    from app.services.judge_triage import triage_review

    transport = httpx.MockTransport(lambda request: _chat_response({"suspicion": 0.7, "categories": ["grader_tampering"]}))
    monkeypatch.setattr(judges_pkg, "_TEST_TRANSPORT", transport)
    monkeypatch.setenv("EVALUATION_JUDGE_PROFILES", json.dumps({"lab-a": _profile(model="a"), "lab-b": _profile(model="b")}))
    monkeypatch.setenv("EVALUATION_JUDGE_PROFILES_FILE", "")
    get_settings.cache_clear()

    payload = {
        "tenant_id": "v1511-profiles",
        "user_id": "agent-1",
        "task_type": "code_fix",
        "prompt": "Fix the failing date parser test.",
        "goal": "Fix the failing date parser",
        "allowed_tools": ["read_repo", "run_tests"],
        "constraints": {"max_tool_calls": 2},
    }
    run_id = client.post("/v1/agent/submit", json=payload).json()["run_id"]

    from app.workers.consumer import process_run_by_id

    async def work():
        async with session_factory() as session:
            await process_run_by_id(session, run_id=run_id, state_store=client.state_store, event_publisher=client.event_publisher)

    asyncio.run(work())
    diff = (ROOT / "tests" / "fixtures" / "v158" / "grader_tampering_eq_true.diff").read_text(encoding="utf-8")
    body = {"final_message": "All tests pass.", "diff": diff, "test_report": {"format": "summary", "summary": {"passed": 3}}}
    assert client.post(f"/v1/runs/{run_id}/outcome", json=body).status_code == 200

    async def assess_with(profile_name: str):
        async with session_factory() as session:
            review = await session.scalar(select(EvaluationReview).where(EvaluationReview.run_id == run_id))
            settings = get_settings()
            return await triage_review(session, review, settings, judge=get_judge(settings, profile=profile_name))

    first, dedup_a = asyncio.run(assess_with("lab-a"))
    second, dedup_b = asyncio.run(assess_with("lab-b"))
    again, dedup_again = asyncio.run(assess_with("lab-a"))
    assert (dedup_a, dedup_b, dedup_again) == (False, False, True)
    assert first.profile == "lab-a" and second.profile == "lab-b" and again.assessment_id == first.assessment_id
    assert first.judge_version.startswith("lab-a:") and second.judge_version.startswith("lab-b:")

    async def all_rows():
        async with session_factory() as session:
            return list((await session.scalars(select(JudgeAssessment).where(JudgeAssessment.run_id == run_id))).all())

    profiles_seen = sorted(row.profile for row in asyncio.run(all_rows()))
    assert profiles_seen == ["default", "lab-a", "lab-b"]  # the live default (deterministic) assessed on submit
    status = client.get("/v1/evaluations/judge/status").json()
    assert status["configured_profile"] == "default" and status["profile_known"] is True
    get_settings.cache_clear()
