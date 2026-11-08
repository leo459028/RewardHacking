"""v1.5.12 Judge Activation & Provider Adapters: activation gate, hardening, adapters, fallback, breaker, budgets."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.models.governance_event import GovernanceEvent
from app.models.judge import JudgeProfileConfig, JudgeProfileState
from app.services import judge_activation as ja
from app.services.evaluation import judges as judges_pkg
from app.services.evaluation.judges import activation_state
from app.services.evaluation.judges import openai_compatible as oc
from app.services.evaluation.judges import profiles as jp
from app.services.evaluation.judges.base import JudgeOutput

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = ROOT / "migrations" / "versions"
FAKE_KEY = "sk-v1512-FAKE-KEY-0123456789abcdef"
GOOD = {"suspicion": 0.4, "categories": ["test_tampering"], "rationale": "assertion removed", "evidence_refs": ["tests/a.py:3"]}


def _load_migration(filename: str):
    spec = importlib.util.spec_from_file_location(filename.removesuffix(".py"), MIGRATIONS_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _settings(**values) -> Settings:
    values.setdefault("evaluation_judge_profiles_file", "")
    return Settings(**values)


def _profile(**overrides) -> dict:
    data = {"provider": "openai_compatible", "base_url": "http://localhost:1234/v1", "model": "qwen3.5-2b", "timeout_seconds": 30}
    data.update(overrides)
    return data


CLOUD = _profile(
    base_url="https://api.example.com/v1",
    model="cloud-judge",
    external=True,
    pricing_provider="google",
    api_key_env="V1512_CLOUD_API_KEY",
    structured_output="json_schema",
)


def _db(session_factory, fn):
    async def scenario():
        async with session_factory() as session:
            return await fn(session)

    return asyncio.run(scenario())


def _content(text: str, *, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"choices": [{"message": {"content": text}}], "usage": {"prompt_tokens": 40, "completion_tokens": 8}})


def _state(name: str, **values) -> activation_state.ProfileState:
    return activation_state.ProfileState(profile_name=name, **values)


@pytest.fixture(autouse=True)
def _fresh_mode_cache():
    oc.reset_mode_cache()
    yield
    oc.reset_mode_cache()


# ---------------------------------------------------------------------------
# P1: migration, models, settings
# ---------------------------------------------------------------------------


def test_v1512_migration_is_single_head_after_v1511():
    module = _load_migration("202611120001_add_v1512_judge_activation.py")
    assert module.revision == "202611120001" and module.down_revision == "202611100001"
    revisions: dict[str, str | None] = {}
    for path in MIGRATIONS_DIR.glob("*.py"):
        mod = _load_migration(path.name)
        revisions[mod.revision] = mod.down_revision
    referenced = {down for down in revisions.values() if down}
    assert sorted(set(revisions) - referenced) == ["202611120001"]


def test_v1512_tables_and_defaults(session_factory):
    async def scenario(session):
        def _describe(sync_session):
            inspector = sa_inspect(sync_session.connection())
            return {
                "configs": {c["name"] for c in inspector.get_columns("judge_profile_configs")},
                "states": {c["name"] for c in inspector.get_columns("judge_profile_states")},
            }

        described = await session.run_sync(_describe)
        session.add(JudgeProfileConfig(name="lab", config={"model": "m"}))
        session.add(JudgeProfileState(profile_name="lab"))
        await session.commit()
        config = (await session.execute(select(JudgeProfileConfig))).scalar_one()
        state = (await session.execute(select(JudgeProfileState))).scalar_one()
        return described, config, state

    described, config, state = _db(session_factory, scenario)
    assert {"name", "config", "version", "created_by", "updated_by", "created_at", "updated_at"} <= described["configs"]
    assert {
        "profile_name",
        "probe_status",
        "probe_error",
        "probe_fingerprint",
        "probed_at",
        "probe_result",
        "active",
        "activation_fingerprint",
        "activated_by",
        "activated_at",
        "updated_at",
    } <= described["states"]
    assert config.version == 1 and config.created_by == ""
    assert state.active is False and state.probe_status == "" and state.probe_result == {} and state.probed_at is None


def test_v1512_settings_defaults_and_validation():
    settings = _settings()
    assert settings.evaluation_judge_activation_refresh_seconds == 30
    assert settings.evaluation_judge_probe_max_age_hours == 24
    assert settings.evaluation_judge_key_env_allowlist == "" and settings.judge_key_env_allowlist == frozenset()
    assert settings.evaluation_judge_breaker_failure_threshold == 3
    assert settings.evaluation_judge_breaker_ttl_seconds == 300
    assert settings.evaluation_judge_dashboard_editing == "auto"
    assert _settings(evaluation_judge_dashboard_editing=" Insecure_Dev ").evaluation_judge_dashboard_editing == "insecure_dev"
    assert _settings(evaluation_judge_key_env_allowlist="LAB_TOKEN, OTHER").judge_key_env_allowlist == {"LAB_TOKEN", "OTHER"}
    for bad in (
        {"evaluation_judge_dashboard_editing": "yes"},
        {"evaluation_judge_breaker_failure_threshold": 0},
        {"evaluation_judge_breaker_ttl_seconds": 0},
        {"evaluation_judge_activation_refresh_seconds": 0},
        {"evaluation_judge_probe_max_age_hours": 0},
        {"evaluation_judge_key_env_allowlist": "lower-case"},
    ):
        with pytest.raises(ValueError):
            Settings(**bad)


# ---------------------------------------------------------------------------
# P1: D10 / D11 hardening, new fields, D18 nulls
# ---------------------------------------------------------------------------


def test_api_key_env_allowlist_for_file_env_and_parser(tmp_path):
    # File source: startup fails.
    path = tmp_path / "profiles.yaml"
    path.write_text("profiles:\n  - name: leak\n    base_url: http://localhost:1234/v1\n    model: m\n    api_key_env: DATABASE_URL\n", encoding="utf-8")
    with pytest.raises(jp.JudgeProfileError, match="_API_KEY"):
        jp.load_profiles(_settings(evaluation_judge_profiles_file=str(path)))
    # Environment JSON source too.
    with pytest.raises(jp.JudgeProfileError, match="_API_KEY"):
        jp.load_profiles(_settings(evaluation_judge_profiles={"leak": _profile(api_key_env="POSTGRES_PASSWORD")}))
    # The low-level parser applies the pattern even without settings.
    with pytest.raises(jp.JudgeProfileError):
        jp.parse_profile("leak", _profile(api_key_env="DATABASE_URL"), source="env")
    # *_API_KEY names, the legacy variable and explicitly allowlisted names are accepted.
    ok = jp.load_profiles(_settings(evaluation_judge_profiles={"pa": _profile(api_key_env="OPENAI_API_KEY")}))
    assert ok["pa"].api_key_env == "OPENAI_API_KEY"
    assert jp.load_profiles(_settings())["default"].api_key_env == "EVALUATION_JUDGE_API_KEY"
    allowed = jp.load_profiles(
        _settings(evaluation_judge_key_env_allowlist="LAB_JUDGE_TOKEN", evaluation_judge_profiles={"pb": _profile(api_key_env="LAB_JUDGE_TOKEN")})
    )
    assert allowed["pb"].api_key_env == "LAB_JUDGE_TOKEN"


@pytest.mark.parametrize(
    ("base_url", "overrides", "message"),
    [
        ("http://api.example.com/v1", {}, "HTTPS"),
        ("http://localhost:1234/v1", {"external": True}, "HTTPS"),
        ("https://169.254.169.254/latest", {}, "metadata"),
        ("http://169.254.169.254/latest", {}, "metadata"),
        ("https://metadata.google.internal/v1", {}, "metadata"),
        ("https://[fd00:ec2::254]/v1", {}, "metadata"),
        ("https://user:pw@api.example.com/v1", {}, "credentials"),
        ("https://api.example.com/v1?x=1", {}, "query"),
        ("https://api.example.com/v1#frag", {}, "query"),
        ("ftp://api.example.com/v1", {}, "http or https"),
    ],
)
def test_base_url_rules_reject(base_url, overrides, message):
    with pytest.raises(jp.JudgeProfileError, match=message):
        jp.load_profiles(_settings(evaluation_judge_profiles={"bad": _profile(base_url=base_url, **overrides)}))


def test_base_url_rules_accept_internal_http_and_external_https():
    profiles = jp.load_profiles(
        _settings(
            evaluation_judge_profiles={
                "lm": _profile(base_url="http://host.docker.internal:12339/v1"),
                "local": _profile(base_url="http://localhost:1234/v1"),
                "cloud": _profile(base_url="https://api.example.com/v1"),
            }
        )
    )
    assert {"lm", "local", "cloud"} <= set(profiles)


def test_legacy_judge_base_url_is_hardened_too():
    with pytest.raises(jp.JudgeProfileError, match="EVALUATION_JUDGE_BASE_URL"):
        jp.load_profiles(_settings(evaluation_judge_provider="openai_compatible", evaluation_judge_base_url="http://api.example.com/v1", evaluation_judge_model="m"))
    assert jp.load_profiles(
        _settings(evaluation_judge_provider="openai_compatible", evaluation_judge_base_url="http://localhost:8001/v1", evaluation_judge_model="m")
    )["default"].base_url == "http://localhost:8001/v1"


def test_fallback_and_daily_cap_fields():
    settings = _settings(
        evaluation_judge_profiles={
            "primary": _profile(fallback=["backup", "deterministic"], daily_token_cap=5000, provider="anthropic", base_url="https://api.anthropic.com"),
            "backup": _profile(),
        }
    )
    primary = jp.get_profile(settings, "primary")
    assert primary.fallback == ("backup",) and primary.daily_token_cap == 5000 and primary.provider == "anthropic"
    assert primary.public_dict(settings)["fallback"] == ["backup"] and jp.get_profile(settings, "backup").daily_token_cap is None
    for bad, message in (
        ({"pa": _profile(fallback=["pa"])}, "itself"),
        ({"pa": _profile(fallback=["pb", "pc", "pd"]), "pb": _profile(), "pc": _profile(), "pd": _profile()}, "at most"),
        ({"pa": _profile(fallback=["pb", "pb"]), "pb": _profile()}, "twice"),
        ({"pa": _profile(fallback=["missing"])}, "not defined"),
        ({"pa": _profile(fallback=["pb"]), "pb": _profile(fallback=["pa"])}, "loops"),
        ({"pa": _profile(daily_token_cap=0)}, "daily_token_cap"),
        ({"pa": _profile(fallback="Not A Name!")}, "not a profile name"),
    ):
        with pytest.raises(jp.JudgeProfileError, match=message):
            jp.load_profiles(_settings(evaluation_judge_profiles=bad))


def test_null_optional_fields_use_system_defaults():
    profile = jp.parse_profile("lab", _profile(timeout_seconds=None, max_retries=None, execution=None, daily_token_cap=None, fallback=None), source="dashboard")
    assert (profile.timeout_seconds, profile.max_retries, profile.execution, profile.daily_token_cap, profile.fallback) == (30.0, 0, "inline", None, ())


# ---------------------------------------------------------------------------
# P1: fingerprints and key status (D5, D22)
# ---------------------------------------------------------------------------


def test_config_fingerprint_tracks_connection_fields_and_key(monkeypatch):
    monkeypatch.delenv("V1512_CLOUD_API_KEY", raising=False)
    settings = _settings()
    base = jp.parse_profile("cloud", CLOUD, source="env", settings=settings)
    fp = jp.config_fingerprint(base, settings)
    assert len(fp) == 64 and fp == jp.config_fingerprint(base, settings)
    for changed in ({"model": "other"}, {"base_url": "https://api.example.org/v1"}, {"structured_output": "json_object"}, {"max_output_tokens": 900}):
        assert jp.config_fingerprint(jp.parse_profile("cloud", {**CLOUD, **changed}, source="env", settings=settings), settings) != fp
    for unchanged in ({"timeout_seconds": 99}, {"pricing_model": "x"}, {"daily_token_cap": 10}, {"execution": "background"}):
        assert jp.config_fingerprint(jp.parse_profile("cloud", {**CLOUD, **unchanged}, source="env", settings=settings), settings) == fp
    monkeypatch.setenv("V1512_CLOUD_API_KEY", FAKE_KEY)
    with_key = jp.config_fingerprint(base, settings)
    assert with_key != fp
    monkeypatch.setenv("V1512_CLOUD_API_KEY", FAKE_KEY + "-rotated")
    assert jp.config_fingerprint(base, settings) not in {fp, with_key}


def test_key_status_never_reveals_key(monkeypatch):
    monkeypatch.setenv("V1512_CLOUD_API_KEY", FAKE_KEY)
    settings = _settings()
    profile = jp.parse_profile("cloud", CLOUD, source="env", settings=settings)
    status = profile.key_status(settings)
    assert status["api_key_env"] == "V1512_CLOUD_API_KEY" and status["configured"] is True
    assert len(status["key_fingerprint"]) == 6 and status["key_fingerprint"] not in FAKE_KEY
    assert FAKE_KEY not in json.dumps(status) and FAKE_KEY not in json.dumps(profile.public_dict(settings))
    monkeypatch.delenv("V1512_CLOUD_API_KEY")
    assert profile.key_status(settings) == {"api_key_env": "V1512_CLOUD_API_KEY", "configured": False, "key_fingerprint": ""}


# ---------------------------------------------------------------------------
# P1: Dashboard-sourced profiles merged into load_profiles (D4)
# ---------------------------------------------------------------------------


def test_dashboard_profiles_merge_after_static_sources(tmp_path):
    path = tmp_path / "profiles.yaml"
    path.write_text("profiles:\n  - name: filed\n    base_url: http://localhost:1234/v1\n    model: m\n", encoding="utf-8")
    settings = _settings(evaluation_judge_profiles_file=str(path))
    activation_state.install(
        {},
        {
            "dash": (1, _profile(model="dash-model", fallback=["filed"])),
            "filed": (1, _profile(model="shadowed")),  # collides with the file profile: file wins
            "broken": (1, _profile(base_url="http://api.example.com/v1")),  # no longer valid: skipped, not fatal
            "orphan": (1, _profile(fallback=["broken"])),  # depends on a skipped row: skipped too
        },
    )
    profiles = jp.load_profiles(settings)
    assert profiles["dash"].source == "dashboard" and profiles["dash"].model == "dash-model"
    assert profiles["filed"].source == "file" and profiles["filed"].model == "m"
    assert "broken" not in profiles and "orphan" not in profiles
    errors = jp.dashboard_profile_errors(settings)
    assert set(errors) == {"filed", "broken", "orphan"} and "HTTPS" in errors["broken"]
    assert "dash" not in jp.static_profiles(settings) and "filed" in jp.static_profiles(settings)
    # A new snapshot version invalidates the cached profile set.
    activation_state.install({}, {"dash": (2, _profile(model="dash-model-2"))})
    assert jp.get_profile(settings, "dash").model == "dash-model-2"


# ---------------------------------------------------------------------------
# P1: active-profile resolution (D1, D2, D6)
# ---------------------------------------------------------------------------


def _env_settings(monkeypatch, profiles: dict, active: str, **extra) -> Settings:
    monkeypatch.setenv("EVALUATION_JUDGE_PROFILES", json.dumps(profiles))
    monkeypatch.setenv("EVALUATION_JUDGE_PROFILES_FILE", "")
    monkeypatch.setenv("EVALUATION_JUDGE_PROFILE", active)
    for key, value in extra.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    return get_settings()


def test_default_is_rule_based(activation_gate):
    resolution = jp.resolve_active(_settings())
    assert resolution.profile.provider == "deterministic" and resolution.source == "default" and not resolution.notes


def test_environment_profile_needs_a_matching_successful_probe(activation_gate, monkeypatch):
    settings = _env_settings(monkeypatch, {"local-small": _profile()}, "local-small")
    profile = jp.get_profile(settings, "local-small")
    fingerprint = jp.config_fingerprint(profile, settings)
    first = jp.resolve_active(settings)
    assert first.profile.name == "deterministic" and first.notes == {"local-small": "not_probed"}
    activation_state.install({"local-small": _state("local-small", probe_status="timeout", probe_fingerprint=fingerprint)}, {})
    assert jp.resolve_active(settings).notes == {"local-small": "probe_failed"}
    activation_state.install({"local-small": _state("local-small", probe_status="ok", probe_fingerprint=fingerprint)}, {})
    ok = jp.resolve_active(settings)
    assert ok.profile.name == "local-small" and ok.source == "environment" and not ok.notes
    # Model changed in the environment: the old probe no longer counts.
    changed = _env_settings(monkeypatch, {"local-small": _profile(model="qwen3.5-9b")}, "local-small")
    stale = jp.resolve_active(changed)
    assert stale.profile.name == "deterministic" and stale.notes == {"local-small": "needs_reprobe"}
    get_settings.cache_clear()


def test_environment_profile_key_rotation_needs_reprobe(activation_gate, monkeypatch):
    monkeypatch.setenv("V1512_CLOUD_API_KEY", FAKE_KEY)
    settings = _env_settings(monkeypatch, {"cloud": CLOUD}, "cloud")
    profile = jp.get_profile(settings, "cloud")
    activation_state.install({"cloud": _state("cloud", probe_status="ok", probe_fingerprint=jp.config_fingerprint(profile, settings))}, {})
    assert jp.resolve_active_profile(settings).name == "cloud"
    monkeypatch.setenv("V1512_CLOUD_API_KEY", FAKE_KEY + "-rotated")
    assert jp.resolve_active(settings).notes == {"cloud": "needs_reprobe"}
    assert jp.resolve_active_profile(settings).name == "deterministic"
    get_settings.cache_clear()


def test_dashboard_activation_wins_and_stale_activation_is_ignored(activation_gate, monkeypatch):
    settings = _env_settings(monkeypatch, {"env-judge": _profile(model="env-model")}, "env-judge")
    env_profile = jp.get_profile(settings, "env-judge")
    env_fp = jp.config_fingerprint(env_profile, settings)
    dash_raw = _profile(model="dash-model")
    activation_state.install({}, {"dash": (1, dash_raw)})
    dash_fp = jp.config_fingerprint(jp.get_profile(settings, "dash"), settings)
    states = {
        "env-judge": _state("env-judge", probe_status="ok", probe_fingerprint=env_fp),
        "dash": _state("dash", probe_status="ok", probe_fingerprint=dash_fp, active=True, activation_fingerprint=dash_fp),
    }
    activation_state.install(states, {"dash": (1, dash_raw)})
    winner = jp.resolve_active(settings)
    assert winner.profile.name == "dash" and winner.source == "dashboard"
    # The Dashboard profile is edited (version 2, new model): its activation no longer matches -> falls through to D2 ②.
    activation_state.install(states, {"dash": (2, _profile(model="dash-model-v2"))})
    fallthrough = jp.resolve_active(settings)
    assert fallthrough.profile.name == "env-judge" and fallthrough.source == "environment"
    assert fallthrough.notes == {"dash": "needs_reprobe"}
    get_settings.cache_clear()


def test_unknown_environment_profile_is_reported(activation_gate, monkeypatch):
    settings = _env_settings(monkeypatch, {}, "ghost")
    resolution = jp.resolve_active(settings)
    assert resolution.profile.name == "deterministic" and resolution.notes == {"ghost": "unknown_profile"}
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# P1: readiness (D8, D9)
# ---------------------------------------------------------------------------


def test_readiness_reasons_local_profile():
    settings = _settings()
    profile = jp.parse_profile("lab", _profile(), source="env", settings=settings)
    fp = jp.config_fingerprint(profile, settings)
    now = datetime.now(timezone.utc)
    assert ja.readiness_reasons(profile, settings, None, pricing_missing=True) == ["not_probed"]
    failed = _state("lab", probe_status="error", probe_fingerprint=fp, probed_at=now)
    assert ja.readiness_reasons(profile, settings, failed, pricing_missing=True) == ["probe_failed"]
    ok = _state("lab", probe_status="ok", probe_fingerprint=fp, probed_at=now)
    assert ja.readiness_reasons(profile, settings, ok, pricing_missing=True) == []  # local: no pricing requirement
    old = _state("lab", probe_status="ok", probe_fingerprint=fp, probed_at=now - timedelta(hours=25))
    assert ja.readiness_reasons(profile, settings, old, pricing_missing=False) == ["probe_expired"]
    assert ja.readiness_reasons(profile, settings, old, pricing_missing=False, check_age=False) == []
    other = _state("lab", probe_status="ok", probe_fingerprint="0" * 64, probed_at=now)
    assert ja.readiness_reasons(profile, settings, other, pricing_missing=False) == ["needs_reprobe"]
    assert ja.readiness_reasons(jp.DETERMINISTIC, settings, None, pricing_missing=True) == []


def test_readiness_reasons_external_profile(monkeypatch):
    monkeypatch.delenv("V1512_CLOUD_API_KEY", raising=False)
    settings = _settings()
    profile = jp.parse_profile("cloud", CLOUD, source="env", settings=settings)
    ok_state = _state("cloud", probe_status="ok", probe_fingerprint=jp.config_fingerprint(profile, settings), probed_at=datetime.now(timezone.utc))
    reasons = ja.readiness_reasons(profile, settings, ok_state, pricing_missing=True)
    assert reasons == ["key_missing", "external_not_allowed", "pricing_or_cap_missing"]
    monkeypatch.setenv("V1512_CLOUD_API_KEY", FAKE_KEY)
    allowed = _settings(evaluation_judge_allow_external=True)
    ok_state = _state("cloud", probe_status="ok", probe_fingerprint=jp.config_fingerprint(profile, allowed), probed_at=datetime.now(timezone.utc))
    assert ja.readiness_reasons(profile, allowed, ok_state, pricing_missing=True) == ["pricing_or_cap_missing"]
    assert ja.readiness_reasons(profile, allowed, ok_state, pricing_missing=False) == []
    capped = jp.parse_profile("cloud", {**CLOUD, "daily_token_cap": 20000}, source="env", settings=allowed)
    assert ja.readiness_reasons(capped, allowed, ok_state, pricing_missing=True) == []


# ---------------------------------------------------------------------------
# P1: probe records, cache refresh, startup auto-probe (D2, D3, D7, D21)
# ---------------------------------------------------------------------------


def _mock_judge(monkeypatch, handler):
    calls: list[dict] = []

    def transport_handler(request: httpx.Request):
        calls.append({"url": str(request.url), "headers": dict(request.headers)})
        return handler(json.loads(request.content))

    monkeypatch.setattr(judges_pkg, "_TEST_TRANSPORT", httpx.MockTransport(transport_handler))
    return calls


def test_probe_records_state_audits_without_key_and_refreshes_cache(session_factory, monkeypatch, activation_gate):
    from app.services.judge_profiles_admin import probe_profile

    monkeypatch.setenv("LOCAL_SMALL_API_KEY", FAKE_KEY)
    settings = _env_settings(monkeypatch, {"local-small": _profile(api_key_env="LOCAL_SMALL_API_KEY")}, "local-small")
    calls = _mock_judge(monkeypatch, lambda body: _content(json.dumps(GOOD)))
    result = _db(session_factory, lambda s: probe_profile(s, settings, "local-small", actor="tester"))
    assert result["status"] == "ok" and len(calls) == 1
    state = _db(session_factory, lambda s: ja.state_row(s, "local-small"))
    profile = jp.get_profile(settings, "local-small")
    assert state.probe_status == "ok" and state.probe_fingerprint == jp.config_fingerprint(profile, settings)
    assert state.probed_at is not None and state.active is False
    assert state.probe_result["input_tokens"] == 40 and state.probe_result["effective_external"] is False
    # Cache refreshed by the write: the environment profile is now in use (probe success is enough for D2 ②).
    assert jp.resolve_active(settings).source == "environment"
    events = _db(session_factory, lambda s: _all(s, select(GovernanceEvent).where(GovernanceEvent.event_type == "evaluation.judge_profile.probed")))
    assert len(events) == 1 and events[0].details["profile"] == "local-small" and events[0].details["actor"] == "tester"
    assert events[0].decision == "ok" and len(events[0].details["fingerprint"]) == 12
    dumped = json.dumps([e.details for e in events]) + json.dumps(result) + json.dumps(state.probe_result)
    assert FAKE_KEY not in dumped
    get_settings.cache_clear()


async def _all(session, stmt):
    return list((await session.scalars(stmt)).all())


def test_maybe_refresh_picks_up_rows_written_elsewhere(session_factory, activation_gate):
    settings = _settings(evaluation_judge_activation_refresh_seconds=3600)

    async def scenario(session):
        await ja.refresh_activation_cache(session)
        session.add(JudgeProfileConfig(name="dash", config=_profile(model="dash-model")))
        await session.commit()
        skipped = await ja.maybe_refresh(session, settings)  # snapshot is fresh: no reload yet
        before = "dash" in jp.load_profiles(settings)
        forced = await ja.maybe_refresh(session, settings, force=True)
        return skipped, before, forced, "dash" in jp.load_profiles(settings)

    assert _db(session_factory, scenario) == (False, False, True, True)


def test_startup_probes_environment_profile_and_uses_it_on_success(session_factory, monkeypatch, activation_gate):
    settings = _env_settings(monkeypatch, {"local-small": _profile()}, "local-small")
    calls = _mock_judge(monkeypatch, lambda body: _content(json.dumps(GOOD)))
    assert ja.startup_probe_needed(settings) == "local-small"

    async def scenario():
        task = await ja.startup(settings, session_factory)
        assert task is not None
        await task

    asyncio.run(scenario())
    assert len(calls) == 1
    resolution = jp.resolve_active(settings)
    assert resolution.profile.name == "local-small" and resolution.source == "environment"
    state = _db(session_factory, lambda s: ja.state_row(s, "local-small"))
    assert state.probe_status == "ok" and state.active is False  # environment profiles need no "activate" click
    events = _db(session_factory, lambda s: _all(s, select(GovernanceEvent).where(GovernanceEvent.event_type == "evaluation.judge_profile.probed")))
    assert events[0].details["actor"] == "system:startup"
    # A second start with the probe on record schedules nothing.
    assert asyncio.run(ja.startup(settings, session_factory)) is None
    get_settings.cache_clear()


def test_startup_probe_failure_keeps_deterministic(session_factory, monkeypatch, activation_gate):
    settings = _env_settings(monkeypatch, {"local-small": _profile()}, "local-small")
    _mock_judge(monkeypatch, lambda body: httpx.Response(503, json={"error": "down"}))

    async def scenario():
        task = await ja.startup(settings, session_factory)
        await task

    asyncio.run(scenario())
    resolution = jp.resolve_active(settings)
    assert resolution.profile.name == "deterministic" and resolution.notes == {"local-small": "probe_failed"}
    state = _db(session_factory, lambda s: ja.state_row(s, "local-small"))
    assert state.probe_status == "error" and state.probe_error == "http_503"
    get_settings.cache_clear()


def test_startup_skips_probe_for_rule_based_or_dashboard_activation(session_factory, monkeypatch, activation_gate):
    assert asyncio.run(ja.startup(_settings(), session_factory)) is None
    settings = _env_settings(monkeypatch, {"local-small": _profile()}, "local-small")
    activation_state.install({"deterministic": _state("deterministic", active=True)}, {})
    assert ja.startup_probe_needed(settings) == ""  # a Dashboard choice is in effect
    get_settings.cache_clear()


def test_judge_status_reports_source_and_notes(client, monkeypatch, activation_gate):
    body = client.get("/v1/evaluations/judge/status").json()
    assert body["source"] == "default" and body["provider"] == "deterministic" and body["activation_notes"] == {}
    _env_settings(monkeypatch, {"local-small": _profile()}, "local-small")
    body = client.get("/v1/evaluations/judge/status").json()
    assert body["provider"] == "deterministic" and body["activation_notes"] == {"local-small": "not_probed"}
    assert body["configured_profile"] == "local-small" and body["used_for_verdict"] is False
    get_settings.cache_clear()


def test_record_probe_never_stores_key(session_factory, monkeypatch):
    monkeypatch.setenv("V1512_CLOUD_API_KEY", FAKE_KEY)
    settings = _settings()
    profile = jp.parse_profile("cloud", CLOUD, source="env", settings=settings)
    output = JudgeOutput(provider="openai_compatible", model="cloud-judge", judge_version="v", status="error", error="http_401")
    row = _db(session_factory, lambda s: ja.record_probe(s, settings, profile, output, actor="t", effective_external_flag=True))
    assert FAKE_KEY not in json.dumps({"fp": row.probe_fingerprint, "err": row.probe_error, "res": row.probe_result})
    assert row.probe_status == "error" and len(row.probe_fingerprint) == 64


# ---------------------------------------------------------------------------
# P2: provider adapters (D12)
# ---------------------------------------------------------------------------

from app.services.evaluation.judges import AnthropicJudge, OpenAICompatibleJudge, build_judge, get_judge  # noqa: E402
from app.services.evaluation.judges.adapters import AnthropicAdapter, OpenAICompatibleAdapter  # noqa: E402
from app.services.evaluation.judges.adapters.anthropic import ANTHROPIC_VERSION, anthropic_output_schema  # noqa: E402
from app.services.evaluation.judges.base import JudgeInput  # noqa: E402

CLAUDE = {
    "provider": "anthropic",
    "base_url": "https://api.anthropic.com",
    "model": "claude-sonnet-5-5",
    "api_key_env": "V1512_ANTHROPIC_API_KEY",
    "external": True,
    "structured_output": "auto",
    "max_output_tokens": 4000,
    "pricing_provider": "anthropic",
    "timeout_seconds": 20,
}
MESSAGES = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "USER"}]


def _judge_input() -> JudgeInput:
    return JudgeInput(goal="Fix the parser", diff="diff --git a/x.py b/x.py\n+x = 1\n", final_message="Done")


def _anthropic_response(text: str | None = None, *, status: int = 200, stop_reason: str = "end_turn", thinking: bool = True, headers=None):
    content = []
    if thinking:
        content.append({"type": "thinking", "thinking": "", "signature": "sig"})
    if text is not None:
        content.append({"type": "text", "text": text})
    body = {"id": "msg_1", "type": "message", "role": "assistant", "content": content, "stop_reason": stop_reason, "usage": {"input_tokens": 120, "output_tokens": 30}}
    return httpx.Response(status, json=body, headers=headers or {})


def _recording(handler):
    calls: list[dict] = []

    def transport_handler(request: httpx.Request):
        body = json.loads(request.content)
        calls.append({"url": str(request.url), "headers": dict(request.headers), "body": body})
        return handler(body, len(calls))

    return httpx.MockTransport(transport_handler), calls


def _claude_judge(monkeypatch, handler, **overrides) -> tuple[AnthropicJudge, list[dict]]:
    monkeypatch.setenv("V1512_ANTHROPIC_API_KEY", FAKE_KEY)
    transport, calls = _recording(handler)
    monkeypatch.setattr(judges_pkg, "_TEST_TRANSPORT", transport)
    settings = _settings(evaluation_judge_allow_external=True, evaluation_judge_profiles={"claude": {**CLAUDE, **overrides}})
    judge = get_judge(settings, profile="claude")
    return judge, calls


def test_openai_adapter_request_matches_v1511_shape():
    adapter = OpenAICompatibleAdapter(base_url="http://localhost:1234/v1/", model="m", api_key="k", max_output_tokens=300, reasoning_effort="none")
    url, headers, body = adapter.build_request(MESSAGES, "json_schema")
    assert url == "http://localhost:1234/v1/chat/completions" and headers["Authorization"] == "Bearer k"
    assert body["temperature"] == 0 and body["max_tokens"] == 300 and body["reasoning_effort"] == "none" and body["messages"] == MESSAGES
    assert body["response_format"]["type"] == "json_schema" and body["response_format"]["json_schema"]["strict"] is True
    assert adapter.build_request(MESSAGES, "json_object")[2]["response_format"] == {"type": "json_object"}
    assert "response_format" not in adapter.build_request(MESSAGES, "none")[2]
    assert "Authorization" not in OpenAICompatibleAdapter(base_url="http://x", model="m").build_request(MESSAGES, "none")[1]
    parsed = adapter.parse_response({"choices": [{"message": {"content": "{}", "reasoning_content": "hmm"}}], "usage": {"prompt_tokens": 5, "completion_tokens": 2}})
    assert (parsed.content, parsed.reasoning, parsed.input_tokens, parsed.output_tokens, parsed.error) == ("{}", "hmm", 5, 2, "")
    assert adapter.parse_response({"choices": []}).error == "missing_choice_content"
    assert adapter.parse_response({"choices": [{"message": {"content": "  "}}]}).error == "empty_content"


def test_build_judge_picks_adapter_by_provider(monkeypatch):
    monkeypatch.setenv("V1512_ANTHROPIC_API_KEY", FAKE_KEY)
    settings = _settings(evaluation_judge_profiles={"claude": CLAUDE, "lab": _profile()})
    claude = build_judge(jp.get_profile(settings, "claude"), settings)
    lab = build_judge(jp.get_profile(settings, "lab"), settings)
    assert isinstance(claude, AnthropicJudge) and isinstance(claude.adapter, AnthropicAdapter) and claude.name == "anthropic"
    assert isinstance(lab, OpenAICompatibleJudge) and isinstance(lab.adapter, OpenAICompatibleAdapter) and lab.name == "openai_compatible"
    assert claude.version().startswith("claude:anthropic:claude-sonnet-5-5:") and claude.is_external is True
    assert FAKE_KEY not in repr(vars(claude)).replace(repr(claude.adapter), "")


def test_anthropic_request_shape(monkeypatch):
    judge, calls = _claude_judge(monkeypatch, lambda body, n: _anthropic_response(json.dumps(GOOD)), reasoning_effort="minimal")
    result = asyncio.run(judge.assess(_judge_input()))
    assert result.status == "ok" and result.suspicion == 0.4 and result.structured_output == "json_schema"
    call = calls[0]
    assert call["url"] == "https://api.anthropic.com/v1/messages"
    assert call["headers"]["x-api-key"] == FAKE_KEY and call["headers"]["anthropic-version"] == ANTHROPIC_VERSION == "2026-10-21"
    assert "authorization" not in call["headers"]
    body = call["body"]
    assert body["model"] == "claude-sonnet-5-5" and body["max_tokens"] == 4000
    assert "temperature" not in body and "tool_choice" not in body and "tools" not in body
    assert body["system"] and [m["role"] for m in body["messages"]] == ["user"]
    assert body["output_config"]["effort"] == "low"
    schema = body["output_config"]["format"]
    assert schema["type"] == "json_schema" and schema["schema"] == anthropic_output_schema()
    flat = json.dumps(schema)
    assert "minimum" not in flat and "maxLength" not in flat and "maxItems" not in flat and '"additionalProperties": false' in flat


def test_anthropic_parses_text_after_thinking_and_records_usage(monkeypatch):
    judge, _ = _claude_judge(monkeypatch, lambda body, n: _anthropic_response(json.dumps(GOOD), thinking=True))
    result = asyncio.run(judge.assess(_judge_input()))
    assert result.status == "ok" and (result.input_tokens, result.output_tokens) == (120, 30)
    assert result.categories == ["test_tampering"] and result.provider == "anthropic" and result.profile == "claude"


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (_anthropic_response(None, stop_reason="refusal"), "refusal"),
        (_anthropic_response(None, stop_reason="max_tokens"), "max_tokens"),
        (_anthropic_response("   "), "empty_content"),
        (httpx.Response(200, json={"type": "message"}), "missing_content"),
    ],
)
def test_anthropic_unusable_answers_are_invalid_output(monkeypatch, response, error):
    judge, _ = _claude_judge(monkeypatch, lambda body, n: response)
    result = asyncio.run(judge.assess(_judge_input()))
    assert result.status == "invalid_output" and result.error == error and result.suspicion is None


def test_anthropic_retries_429_and_529_with_retry_after(monkeypatch):
    def handler(body, n):
        if n == 1:
            return httpx.Response(429, json={"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}}, headers={"Retry-After": "0"})
        if n == 2:
            return httpx.Response(529, json={"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}, headers={"Retry-After": "0"})
        return _anthropic_response(json.dumps(GOOD))

    judge, calls = _claude_judge(monkeypatch, handler, max_retries=2)
    result = asyncio.run(judge.assess(_judge_input()))
    assert result.status == "ok" and result.attempts == 3 and len(calls) == 3


def test_anthropic_without_retries_reports_http_error(monkeypatch):
    judge, calls = _claude_judge(monkeypatch, lambda body, n: httpx.Response(401, json={"type": "error", "error": {"type": "authentication_error"}}))
    result = asyncio.run(judge.assess(_judge_input()))
    assert result.status == "error" and result.error == "http_401" and len(calls) == 1
    assert FAKE_KEY not in json.dumps(result.summary())


def test_anthropic_auto_degrades_when_output_format_is_rejected(monkeypatch):
    def handler(body, n):
        if "output_config" in body and "format" in body["output_config"]:
            return httpx.Response(400, json={"type": "error", "error": {"type": "invalid_request_error", "message": "output_config.format is not supported"}})
        return _anthropic_response(json.dumps(GOOD), thinking=False)

    judge, calls = _claude_judge(monkeypatch, handler)
    result = asyncio.run(judge.assess(_judge_input()))
    assert result.status == "ok" and result.structured_output == "none" and len(calls) == 2
    assert oc.cached_mode("https://api.anthropic.com", "claude-sonnet-5-5") == "none"


def test_anthropic_explicit_json_object_is_sent_as_prompt_only(monkeypatch):
    judge, calls = _claude_judge(monkeypatch, lambda body, n: _anthropic_response(json.dumps(GOOD)), structured_output="json_object")
    assert asyncio.run(judge.assess(_judge_input())).structured_output == "none"
    assert "output_config" not in calls[0]["body"]


def test_anthropic_base_url_with_v1_suffix():
    assert AnthropicAdapter(base_url="https://api.anthropic.com/v1/", model="m").url == "https://api.anthropic.com/v1/messages"
    assert AnthropicAdapter(base_url="https://gateway.example.com/anthropic", model="m").url == "https://gateway.example.com/anthropic/v1/messages"


def test_anthropic_external_needs_global_switch(monkeypatch):
    monkeypatch.setenv("V1512_ANTHROPIC_API_KEY", FAKE_KEY)
    transport, calls = _recording(lambda body, n: _anthropic_response(json.dumps(GOOD)))
    monkeypatch.setattr(judges_pkg, "_TEST_TRANSPORT", transport)
    settings = _settings(evaluation_judge_profiles={"claude": CLAUDE})
    result = asyncio.run(get_judge(settings, profile="claude").assess(_judge_input()))
    assert result.status == "blocked_external" and calls == []


def test_request_time_url_guard_blocks_insecure_external_urls(monkeypatch):
    transport, calls = _recording(lambda body, n: _content(json.dumps(GOOD)))
    for url in ("http://api.example.com/v1", "https://169.254.169.254/v1", "https://user:pw@api.example.com/v1"):
        judge = OpenAICompatibleJudge(base_url=url, model="m", allow_external=True, transport=transport)
        result = asyncio.run(judge.assess(_judge_input()))
        assert result.status == "blocked_insecure_url" and result.error == "insecure_base_url"
    assert calls == []
    ok = OpenAICompatibleJudge(base_url="http://localhost:1234/v1", model="m", transport=transport)
    assert asyncio.run(ok.assess(_judge_input())).status == "ok"
