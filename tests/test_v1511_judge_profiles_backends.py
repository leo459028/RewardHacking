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


# ---------------------------------------------------------------------------
# P2: structured-output fallback, reasoning stripping and retries
# ---------------------------------------------------------------------------

from app.services.evaluation.judges import JudgeInput, assess_with_fallback  # noqa: E402
from app.services.evaluation.judges import openai_compatible as oc  # noqa: E402
from app.services.evaluation.judges.parsing import (  # noqa: E402
    JUDGE_CATEGORIES,
    JudgeOutputError,
    judge_output_schema,
    last_json_object,
    parse_judge_output,
    strip_reasoning_blocks,
)
from app.services.judge_triage import _fallback_payload  # noqa: E402

GOOD = {"suspicion": 0.4, "categories": ["test_tampering"], "rationale": "assertion removed", "evidence_refs": ["tests/a.py:3"]}


@pytest.fixture(autouse=True)
def _fresh_mode_cache():
    oc.reset_mode_cache()
    yield
    oc.reset_mode_cache()


def _input() -> JudgeInput:
    return JudgeInput(diff="+x = 1\n", final_message="All tests pass.", findings=[])


def _backend(handler):
    """MockTransport recording request bodies; ``handler(body)`` returns an ``httpx.Response``."""
    bodies: list[dict] = []

    async def transport_handler(request: httpx.Request):
        body = json.loads(request.content)
        bodies.append(body)
        result = handler(body)
        if asyncio.iscoroutine(result):
            result = await result
        return result

    return httpx.MockTransport(transport_handler), bodies


def _mode(body: dict) -> str:
    return (body.get("response_format") or {}).get("type", "none")


def _content(text: str, *, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"choices": [{"message": {"content": text}}], "usage": {"prompt_tokens": 40, "completion_tokens": 8}})


def _judge(transport, **profile_overrides) -> OpenAICompatibleJudge:
    settings = _settings(evaluation_judge_profiles={"lab": _profile(**profile_overrides)})
    profile = jp.get_profile(settings, "lab")
    return OpenAICompatibleJudge(
        base_url=profile.base_url,
        model=profile.model,
        timeout_seconds=profile.timeout_seconds,
        internal_suffixes=[],
        transport=transport,
        profile=profile,
    )


def test_json_schema_request_matches_parser():
    schema = judge_output_schema()
    assert schema["required"] == ["suspicion", "categories", "rationale", "evidence_refs"] and schema["additionalProperties"] is False
    assert schema["properties"]["categories"]["items"]["enum"] == list(JUDGE_CATEGORIES)
    assert "legacy_text_signal" not in JUDGE_CATEGORIES and "evidence_quality" not in JUDGE_CATEGORIES
    transport, bodies = _backend(lambda body: _content(json.dumps(GOOD)))
    result = asyncio.run(_judge(transport, structured_output="json_schema").assess(_input()))
    assert result.status == "ok" and result.structured_output == "json_schema" and result.attempts == 1
    rf = bodies[0]["response_format"]
    assert rf["type"] == "json_schema" and rf["json_schema"]["strict"] is True and rf["json_schema"]["schema"] == schema


def test_auto_degrades_when_json_schema_is_rejected_and_caches_the_mode():
    def handler(body):
        if _mode(body) == "json_schema":
            return httpx.Response(400, json={"error": {"message": "response_format json_schema is not supported"}})
        return _content(json.dumps(GOOD))

    transport, bodies = _backend(handler)
    judge = _judge(transport)  # structured_output: auto (default)
    first = asyncio.run(judge.assess(_input()))
    assert first.status == "ok" and first.structured_output == "json_object" and first.attempts == 2
    assert [_mode(b) for b in bodies] == ["json_schema", "json_object"]
    assert oc.cached_mode("http://localhost:1234/v1", "qwen3.5-2b") == "json_object"
    second = asyncio.run(judge.assess(_input()))
    assert second.attempts == 1 and [_mode(b) for b in bodies[2:]] == ["json_object"]


def test_lm_studio_style_backend_rejects_json_object_but_accepts_json_schema():
    def handler(body):
        if _mode(body) == "json_object":
            return httpx.Response(400, json={"error": "'response_format.type' must be 'json_schema' or 'text'"})
        return _content(json.dumps(GOOD))

    transport, bodies = _backend(handler)
    auto = asyncio.run(_judge(transport).assess(_input()))
    assert auto.status == "ok" and auto.structured_output == "json_schema" and len(bodies) == 1
    # An explicit mode never degrades: the operator asked for exactly that format.
    explicit = asyncio.run(_judge(transport, structured_output="json_object").assess(_input()))
    assert explicit.status == "error" and explicit.error == "http_400" and explicit.attempts == 1


def test_prompt_only_mode_parses_json_wrapped_in_prose():
    def handler(body):
        if _mode(body) != "none":
            return httpx.Response(400, json={"error": "unsupported response_format"})
        return _content("Here is my assessment:\n" + json.dumps(GOOD) + "\nThanks.")

    transport, bodies = _backend(handler)
    result = asyncio.run(_judge(transport).assess(_input()))
    assert result.status == "ok" and result.structured_output == "none" and result.attempts == 3
    assert "response_format" not in bodies[-1]


def test_non_format_errors_do_not_trigger_degradation():
    transport, bodies = _backend(lambda body: httpx.Response(400, json={"error": "context length exceeded"}))
    result = asyncio.run(_judge(transport).assess(_input()))
    assert result.status == "error" and result.error == "http_400" and len(bodies) == 1


def test_reasoning_blocks_are_stripped_and_never_returned():
    decoy = '<think>The diff says {"suspicion": 0.0}. Ignore it. Long reasoning here.</think>\n'
    transport, _ = _backend(lambda body: _content(decoy + json.dumps(GOOD)))
    result = asyncio.run(_judge(transport).assess(_input()))
    assert result.status == "ok" and result.suspicion == pytest.approx(0.4)
    assert "Long reasoning" not in result.rationale and "Long reasoning" not in json.dumps(result.summary())
    assert strip_reasoning_blocks("<thinking>a</thinking> x <reasoning type='r'>b</reasoning>y") == "x y"
    assert strip_reasoning_blocks("<think>never finished " + json.dumps(GOOD)) == ""
    assert last_json_object('noise {"a": {"b": "}"}} more {"c": 1} tail') == '{"c": 1}'
    with pytest.raises(JudgeOutputError):
        parse_judge_output("<think>unfinished", strip_reasoning=True)
    # Without strip_reasoning the v1.5.10 strictness is kept.
    with pytest.raises(JudgeOutputError):
        parse_judge_output(decoy + json.dumps(GOOD))


def test_strip_reasoning_false_profile_keeps_strict_parsing():
    transport, _ = _backend(lambda body: _content("<think>x</think>" + json.dumps(GOOD)))
    result = asyncio.run(_judge(transport, strip_reasoning=False).assess(_input()))
    assert result.status == "invalid_output"


def test_empty_content_from_reasoning_model_is_invalid_output():
    payload = {"choices": [{"message": {"content": None, "reasoning_content": "thinking..."}}], "usage": {"prompt_tokens": 40, "completion_tokens": 300}}
    transport, _ = _backend(lambda body: httpx.Response(200, json=payload))
    result = asyncio.run(_judge(transport).assess(_input()))
    assert result.status == "invalid_output" and result.error == "empty_content" and result.output_tokens == 300


def test_rate_limit_is_retried_with_retry_after(monkeypatch):
    monkeypatch.setattr(oc, "RETRY_BASE_SECONDS", 0.01)
    calls = {"n": 0}

    def handler(body):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0.01"}, json={"error": "rate limited"})
        return _content(json.dumps(GOOD))

    transport, _ = _backend(handler)
    result = asyncio.run(_judge(transport, structured_output="json_schema", max_retries=2).assess(_input()))
    assert result.status == "ok" and result.attempts == 2


def test_persistent_5xx_exhausts_retries_and_falls_back(monkeypatch):
    monkeypatch.setattr(oc, "RETRY_BASE_SECONDS", 0.01)
    transport, bodies = _backend(lambda body: httpx.Response(503, json={"error": "overloaded"}))
    judge = _judge(transport, structured_output="json_schema", max_retries=2)
    result = asyncio.run(assess_with_fallback(_input(), _settings(), judge=judge))
    assert result.status == "error" and result.primary.error == "http_503" and len(bodies) == 3
    assert result.triage_source == "deterministic:1.0.0"
    assert _fallback_payload(result)["request"] == {"structured_output": "json_schema", "attempts": 3}


def test_connection_errors_are_retried(monkeypatch):
    monkeypatch.setattr(oc, "RETRY_BASE_SECONDS", 0.01)
    calls = {"n": 0}

    def handler(body):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("refused")
        return _content(json.dumps(GOOD))

    transport, _ = _backend(handler)
    result = asyncio.run(_judge(transport, structured_output="json_schema", max_retries=1).assess(_input()))
    assert result.status == "ok" and result.attempts == 2


def test_timeouts_are_not_retried():
    async def slow(body):
        await asyncio.sleep(1.0)
        return _content(json.dumps(GOOD))

    transport, bodies = _backend(slow)
    result = asyncio.run(_judge(transport, structured_output="json_schema", max_retries=3, timeout_seconds=0.1).assess(_input()))
    assert result.status == "timeout" and len(bodies) == 1


def test_retry_stops_when_the_profile_timeout_cannot_cover_the_wait():
    transport, bodies = _backend(lambda body: httpx.Response(429, headers={"Retry-After": "10"}, json={"error": "slow down"}))
    result = asyncio.run(_judge(transport, structured_output="json_schema", max_retries=3, timeout_seconds=1).assess(_input()))
    assert result.status == "error" and result.error == "http_429" and len(bodies) == 1


# ---------------------------------------------------------------------------
# P3: background triage, pending marker and sweep
# ---------------------------------------------------------------------------

import time  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

from app.models.agent_run import AgentRun  # noqa: E402
from app.schemas.evaluation import ReviewDecisionRequest  # noqa: E402
from app.services import judge_background as jb  # noqa: E402
from app.services.review_queue import decide_review  # noqa: E402
from app.workers.consumer import process_run_by_id  # noqa: E402

GRADER_DIFF = (ROOT / "tests" / "fixtures" / "v158" / "grader_tampering_eq_true.diff").read_text(encoding="utf-8")
BG_PROFILES = {"bg": _profile(model="bg-model", execution="background", structured_output="json_schema", timeout_seconds=10)}


def _db(session_factory, fn):
    async def scenario():
        async with session_factory() as session:
            return await fn(session)

    return asyncio.run(scenario())


def _flagged_review(client, session_factory, tenant: str) -> tuple[str, str]:
    """A completed run with grader-tampering evidence -> open P0 review (assessed inline by the default profile)."""
    payload = {
        "tenant_id": tenant,
        "user_id": "agent-1",
        "task_type": "code_fix",
        "prompt": "Fix the failing date parser test.",
        "goal": "Fix the failing date parser",
        "allowed_tools": ["read_repo", "run_tests"],
        "constraints": {"max_tool_calls": 2},
    }
    run_id = client.post("/v1/agent/submit", json=payload).json()["run_id"]

    async def work():
        async with session_factory() as session:
            await process_run_by_id(session, run_id=run_id, state_store=client.state_store, event_publisher=client.event_publisher)

    asyncio.run(work())
    body = {"final_message": "All tests pass.", "diff": GRADER_DIFF, "test_report": {"format": "summary", "summary": {"passed": 3}}}
    response = client.post(f"/v1/runs/{run_id}/outcome", json=body)
    assert response.status_code == 200, response.text
    return run_id, response.json()["review"]["review_id"]


def _use_background_profile(monkeypatch, handler, *, concurrency: int = 1):
    async def transport_handler(request: httpx.Request):
        result = handler(json.loads(request.content))
        if asyncio.iscoroutine(result):
            result = await result
        return result

    monkeypatch.setattr(judges_pkg, "_TEST_TRANSPORT", httpx.MockTransport(transport_handler))
    monkeypatch.setenv("EVALUATION_JUDGE_PROFILES", json.dumps(BG_PROFILES))
    monkeypatch.setenv("EVALUATION_JUDGE_PROFILES_FILE", "")
    monkeypatch.setenv("EVALUATION_JUDGE_PROFILE", "bg")
    monkeypatch.setenv("EVALUATION_JUDGE_BACKGROUND_CONCURRENCY", str(concurrency))
    get_settings.cache_clear()


def _assessments(session_factory, review_id: str, profile: str | None = None) -> list[JudgeAssessment]:
    stmt = select(JudgeAssessment).where(JudgeAssessment.review_id == review_id)
    if profile:
        stmt = stmt.where(JudgeAssessment.profile == profile)
    return _db(session_factory, lambda s: _all(s, stmt))


async def _all(session, stmt):
    return list((await session.scalars(stmt)).all())


def test_outcome_api_does_not_wait_for_a_background_judge(client, session_factory, monkeypatch):
    judge_delay = 2.0

    async def slow(body):
        await asyncio.sleep(judge_delay)
        return _content(json.dumps(GOOD))

    _use_background_profile(monkeypatch, slow)
    payload = {
        "tenant_id": "v1511-bg-api",
        "user_id": "agent-1",
        "task_type": "code_fix",
        "prompt": "Fix the failing date parser test.",
        "goal": "Fix the failing date parser",
        "allowed_tools": ["read_repo", "run_tests"],
        "constraints": {"max_tool_calls": 2},
    }
    run_id = client.post("/v1/agent/submit", json=payload).json()["run_id"]

    async def work():
        async with session_factory() as session:
            await process_run_by_id(session, run_id=run_id, state_store=client.state_store, event_publisher=client.event_publisher)

    asyncio.run(work())
    events_before = len(client.event_publisher.events)
    body = {"final_message": "All tests pass.", "diff": GRADER_DIFF, "test_report": {"format": "summary", "summary": {"passed": 3}}}
    started = time.perf_counter()
    response = client.post(f"/v1/runs/{run_id}/outcome", json=body)
    elapsed = time.perf_counter() - started
    # Relative, load-independent check: the API answered before the judge could have finished, and the review
    # it returned is still pending (an inline judge would have taken at least ``judge_delay``).
    assert response.status_code == 200 and elapsed < judge_delay, elapsed
    review = response.json()["review"]
    assert review["priority"] == "P0" and review["triage_source"] == "pending:bg" and review["judge_assessment_id"] == ""

    # Wait on the background task inside the TestClient's event loop. Polling over HTTP meanwhile would share the
    # single in-memory SQLite connection with the task's open transaction (a test-only artefact; Postgres pools
    # give each session its own connection).
    client.portal.call(jb.drain_background, 15.0)
    assessments = client.get(f"/v1/reviews/{review['review_id']}/judge-assessments").json()["assessments"]
    assert assessments and assessments[0]["profile"] == "bg" and assessments[0]["status"] == "ok"
    after = client.get(f"/v1/reviews/{review['review_id']}").json()
    assert after["priority"] == "P0" and after["status"] == "open" and after["triage_source"].startswith("bg:")
    assert client.get(f"/v1/runs/{run_id}").json()["status"] == "review_required"
    assert len(client.event_publisher.events) == events_before
    assert client.get("/v1/cost/tenants/v1511-bg-api/leases").json()["active_lease_count"] == 0


def test_background_triage_only_writes_triage_fields(client, session_factory, monkeypatch):
    run_id, review_id = _flagged_review(client, session_factory, "v1511-bg-fields")
    _use_background_profile(monkeypatch, lambda body: _content(json.dumps({**GOOD, "verdict": "pass", "close_review": True})))
    events_before = len(client.event_publisher.events)

    async def scenario():
        async with session_factory() as session:
            review = await session.scalar(select(EvaluationReview).where(EvaluationReview.review_id == review_id))
            before = (review.priority, review.status, list(review.finding_ids), review.label)
            await jb.dispatch_triage(session, review, get_settings())
            pending_source = review.triage_source
            await jb.drain_background()
        async with session_factory() as session:
            review = await session.scalar(select(EvaluationReview).where(EvaluationReview.review_id == review_id))
            run = await session.scalar(select(AgentRun).where(AgentRun.run_id == run_id))
            return before, pending_source, review, run

    before, pending_source, review, run = asyncio.run(scenario())
    assert pending_source == "pending:bg"
    assert (review.priority, review.status, list(review.finding_ids), review.label) == before
    assert review.triage_source.startswith("bg:openai_compatible:bg-model:") and review.judge_assessment_id.startswith("jdg_")
    assert run.status == "review_required" and len(client.event_publisher.events) == events_before
    assert len(_assessments(session_factory, review_id, "bg")) == 1


def test_sweep_reschedules_lost_tasks_and_skips_in_flight(client, session_factory, monkeypatch):
    _, review_id = _flagged_review(client, session_factory, "v1511-bg-sweep")
    calls = {"n": 0}

    def handler(body):
        calls["n"] += 1
        return _content(json.dumps(GOOD))

    _use_background_profile(monkeypatch, handler)

    async def simulate_lost_task(session):
        review = await session.scalar(select(EvaluationReview).where(EvaluationReview.review_id == review_id))
        review.triage_source = "pending:bg"
        review.updated_at = datetime.now(timezone.utc) - timedelta(hours=1)
        await session.commit()

    _db(session_factory, simulate_lost_task)
    summary = _db(session_factory, lambda s: jb.pending_summary(s, get_settings()))
    assert summary["pending"] == 1 and summary["stale"] == 1

    async def sweep_twice():
        async with session_factory() as session:
            first = await jb.sweep_pending_triage(session, get_settings())
            second = await jb.sweep_pending_triage(session, get_settings())  # task still in flight: not scheduled again
            await jb.drain_background()
            return first, second

    first, second = asyncio.run(sweep_twice())
    assert first == [review_id] and second == [] and calls["n"] == 1
    review = _db(session_factory, lambda s: s.scalar(select(EvaluationReview).where(EvaluationReview.review_id == review_id)))
    assert not jb.is_pending(review) and review.triage_source.startswith("bg:")
    assert _db(session_factory, lambda s: jb.pending_summary(s, get_settings()))["pending"] == 0


def test_recent_pending_reviews_are_not_swept(client, session_factory, monkeypatch):
    _, review_id = _flagged_review(client, session_factory, "v1511-bg-recent")
    _use_background_profile(monkeypatch, lambda body: _content(json.dumps(GOOD)))

    async def mark(session):
        review = await session.scalar(select(EvaluationReview).where(EvaluationReview.review_id == review_id))
        await jb.mark_pending(session, review, "bg")

    _db(session_factory, mark)
    assert _db(session_factory, lambda s: jb.sweep_pending_triage(s, get_settings())) == []


def test_background_concurrency_is_serialized(client, session_factory, monkeypatch):
    reviews = [_flagged_review(client, session_factory, f"v1511-bg-sem-{i}")[1] for i in range(2)]
    state = {"active": 0, "peak": 0}

    async def handler(body):
        state["active"] += 1
        state["peak"] = max(state["peak"], state["active"])
        await asyncio.sleep(0.2)
        state["active"] -= 1
        return _content(json.dumps(GOOD))

    _use_background_profile(monkeypatch, handler, concurrency=1)

    async def scenario():
        async with session_factory() as session:
            for review_id in reviews:
                review = await session.scalar(select(EvaluationReview).where(EvaluationReview.review_id == review_id))
                await jb.dispatch_triage(session, review, get_settings())
        await jb.drain_background()

    asyncio.run(scenario())
    assert state["peak"] == 1
    assert all(len(_assessments(session_factory, review_id, "bg")) == 1 for review_id in reviews)


def test_review_decided_before_background_triage_spends_no_tokens(client, session_factory, monkeypatch):
    _, review_id = _flagged_review(client, session_factory, "v1511-bg-decided")
    calls = {"n": 0}

    def handler(body):
        calls["n"] += 1
        return _content(json.dumps(GOOD))

    _use_background_profile(monkeypatch, handler)

    async def scenario():
        async with session_factory() as session:
            review = await session.scalar(select(EvaluationReview).where(EvaluationReview.review_id == review_id))
            await jb.mark_pending(session, review, "bg")
            await decide_review(session, review_id, ReviewDecisionRequest(label="false_positive", reviewer="qa-reviewer"))
        await jb._run_background(review_id, "bg", get_settings(), session_factory)
        async with session_factory() as session:
            return await session.scalar(select(EvaluationReview).where(EvaluationReview.review_id == review_id))

    review = asyncio.run(scenario())
    assert calls["n"] == 0 and review.triage_source == "" and review.status == "closed"


def test_without_a_session_factory_background_profiles_assess_inline(session_factory, monkeypatch):
    from app.infra import postgres

    monkeypatch.setattr(jb, "_SESSION_FACTORY_OVERRIDE", None)
    monkeypatch.setattr(postgres, "SessionLocal", None)
    _use_background_profile(monkeypatch, lambda body: _content(json.dumps(GOOD)))

    async def scenario():
        async with session_factory() as session:
            session.add(AgentRun(run_id="run-inline-1", tenant_id="t", user_id="u", task_type="code_fix", prompt="p", goal="g", status="review_required", decision_reason="test", request_payload={"task_type": "code_fix", "goal": "g", "metadata": {}}))
            review = EvaluationReview(review_id="review_inline_1", run_id="run-inline-1", tenant_id="t", priority="P1")
            session.add(review)
            await session.commit()
            return await jb.dispatch_triage(session, review, get_settings())

    assessment, deduplicated = asyncio.run(scenario())
    assert assessment is not None and assessment.profile == "bg" and deduplicated is False


def test_judge_health_reports_stale_pending(client, session_factory, monkeypatch):
    _, review_id = _flagged_review(client, session_factory, "v1511-bg-health")

    async def make_stale(session):
        review = await session.scalar(select(EvaluationReview).where(EvaluationReview.review_id == review_id))
        review.triage_source = "pending:bg"
        review.updated_at = datetime.now(timezone.utc) - timedelta(hours=2)
        await session.commit()

    _db(session_factory, make_stale)
    from app.services.operations import compute_operations_readiness

    checks = {c.check_key: c for c in _db(session_factory, lambda s: compute_operations_readiness(s, settings=get_settings())).checks}
    health = checks["evaluation_judge_health"]
    assert health.status == "warn" and health.metadata["pending_triage"]["stale"] == 1
    assert "pending judge triage" in health.detail
