"""v1.6.3 P1: secure defaults (plan D15-D17): AUTH_ENABLED defaults to true, the Dashboard sign-in mode defaults to
session, production-like environments refuse Dashboard mode off, and the image / templates / guide match.

Negative cases: an explicit DASHBOARD_AUTH_ENABLED=false still means off, dev / test may run with everything off, and
production with session mode starts normally.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_event_publisher, get_state_store
from app.core.config import Settings, get_settings
from app.infra.postgres import get_session
from app.main import app
from app.services import startup_guard
from app.services.events import InMemoryEventPublisher
from app.services.run_state import InMemoryRunStateStore

SETTINGS_KEYS = ["APP_ENV", "AUTH_ENABLED", "API_KEYS", "DASHBOARD_AUTH_MODE", "DASHBOARD_AUTH_ENABLED", "QUOTA_ENABLED"]


def _read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def _clear(monkeypatch) -> None:
    for key in SETTINGS_KEYS:
        monkeypatch.delenv(key, raising=False)
    # A developer's local .env must not decide these tests (CI has none).
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    get_settings.cache_clear()


def _env(monkeypatch, **env) -> None:
    _clear(monkeypatch)
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))
    get_settings.cache_clear()


@contextmanager
def _client(session_factory):
    async def override_get_session():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_state_store] = InMemoryRunStateStore
    app.dependency_overrides[get_event_publisher] = InMemoryEventPublisher
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# D16: AUTH_ENABLED defaults to true
# ---------------------------------------------------------------------------


def test_auth_enabled_defaults_to_true(monkeypatch):
    _clear(monkeypatch)
    assert Settings(_env_file=None).auth_enabled is True


def test_explicit_auth_enabled_false_is_kept(monkeypatch):
    _env(monkeypatch, AUTH_ENABLED="false")
    assert Settings(_env_file=None).auth_enabled is False


def test_the_suite_pins_auth_off_in_conftest():
    text = _read("tests/conftest.py")
    assert 'os.environ["AUTH_ENABLED"] = "false"' in text


# ---------------------------------------------------------------------------
# D17: Dashboard sign-in mode derivation (environment variables, as deployments set them)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, "session"),  # neither set: secure default
        ({"DASHBOARD_AUTH_ENABLED": "false"}, "off"),  # the older flag set explicitly still derives the mode
        ({"DASHBOARD_AUTH_ENABLED": "true"}, "session"),
        ({"DASHBOARD_AUTH_MODE": "off"}, "off"),
        ({"DASHBOARD_AUTH_MODE": "session"}, "session"),
        ({"DASHBOARD_AUTH_MODE": "session", "DASHBOARD_AUTH_ENABLED": "false"}, "session"),  # the mode wins
        ({"DASHBOARD_AUTH_MODE": " Off ", "DASHBOARD_AUTH_ENABLED": "true"}, "off"),
        ({"DASHBOARD_AUTH_MODE": ""}, "session"),  # empty counts as unset
    ],
)
def test_dashboard_mode_derivation_from_environment(monkeypatch, env, expected):
    _env(monkeypatch, **env)
    assert Settings(_env_file=None).dashboard_auth_mode == expected


def test_dashboard_mode_derivation_from_keyword_arguments(monkeypatch):
    _clear(monkeypatch)
    assert Settings(_env_file=None).dashboard_auth_mode == "session"
    assert Settings(_env_file=None, dashboard_auth_enabled=False).dashboard_auth_mode == "off"
    assert Settings(_env_file=None, dashboard_auth_enabled=True).dashboard_auth_mode == "session"


# ---------------------------------------------------------------------------
# D17: production-like environments refuse Dashboard mode off
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "env",
    [
        {"DASHBOARD_AUTH_MODE": "off"},
        {"DASHBOARD_AUTH_ENABLED": "false"},  # the older flag, mode unset
    ],
)
@pytest.mark.parametrize("app_env", ["prod", "staging"])
def test_api_refuses_to_start_with_dashboard_off_in_production(monkeypatch, session_factory, env, app_env):
    _env(monkeypatch, APP_ENV=app_env, AUTH_ENABLED="true", **env)
    with pytest.raises(startup_guard.InsecureStartupConfiguration) as excinfo:
        with _client(session_factory):
            pass
    assert len(excinfo.value.reasons) == 1
    message = str(excinfo.value)
    assert "DASHBOARD_AUTH_MODE=session" in message and "app.jobs.users create" in message and "APP_ENV=dev" in message


def test_production_with_default_settings_starts(monkeypatch, session_factory):
    _env(monkeypatch, APP_ENV="prod")  # AUTH_ENABLED and the Dashboard mode take their secure defaults
    with _client(session_factory) as client:
        assert client.get("/v1/healthz").status_code == 200
        assert client.get("/v1/metrics").status_code == 401
        redirect = client.get("/dashboard", follow_redirects=False)
    assert redirect.status_code == 303 and redirect.headers["location"].startswith("/dashboard/login")


@pytest.mark.parametrize("app_env", ["dev", "test"])
def test_dev_and_test_may_run_with_dashboard_off(monkeypatch, session_factory, app_env):
    _env(monkeypatch, APP_ENV=app_env, AUTH_ENABLED="false", DASHBOARD_AUTH_MODE="off")
    with _client(session_factory) as client:
        assert client.get("/dashboard", follow_redirects=False).status_code == 200


def test_both_refusals_are_reported_together():
    settings = Settings(_env_file=None, app_env="production", auth_enabled=False, dashboard_auth_mode="off")
    reasons = startup_guard.startup_refusal_reasons(settings)
    assert len(reasons) == 2
    assert "AUTH_ENABLED=true" in reasons[0] and "DASHBOARD_AUTH_MODE=session" in reasons[1]


@pytest.mark.parametrize("app_env", ["dev", "test", " DEV "])
def test_no_refusal_in_dev_and_test_even_with_everything_off(app_env):
    settings = Settings(_env_file=None, app_env=app_env, auth_enabled=False, dashboard_auth_mode="off")
    assert startup_guard.startup_refusal_reasons(settings) == []


def test_refusal_message_never_contains_a_configured_key():
    secret = "very-" + "secret-env-key"
    settings = Settings(_env_file=None, app_env="prod", auth_enabled=False, dashboard_auth_mode="off", api_keys=f"{secret}:t:admin")
    assert secret not in " ".join(startup_guard.startup_refusal_reasons(settings))


def test_from_source_without_env_file_requires_keys_and_sign_in(monkeypatch, session_factory):
    """Plan section 2: no .env at all (APP_ENV defaults to dev): /v1 needs a key, the Dashboard needs a sign-in."""
    _clear(monkeypatch)
    settings = get_settings()
    assert (settings.app_env, settings.auth_enabled, settings.api_keys, settings.dashboard_auth_mode) == ("dev", True, "", "session")
    with _client(session_factory) as client:
        denied = client.get("/v1/metrics")
        redirect = client.get("/dashboard", follow_redirects=False)
    assert denied.status_code == 401 and denied.json()["error"]["code"] == "auth_required"
    assert redirect.status_code == 303 and redirect.headers["location"].startswith("/dashboard/login")


# ---------------------------------------------------------------------------
# D15 / D17 / D10: image, template and guide
# ---------------------------------------------------------------------------


def _runtime_stage(dockerfile: str) -> str:
    return re.split(r"(?m)^FROM ", dockerfile)[-1]


def test_dockerfile_runtime_stage_defaults_to_production():
    dockerfile = _read("Dockerfile")
    runtime = _runtime_stage(dockerfile)
    assert runtime.startswith("python:") and " AS runtime" in runtime.splitlines()[0]
    assert re.search(r"(?m)^ENV APP_ENV=prod$", runtime)
    assert dockerfile.count("APP_ENV=") == 1  # set once, in the final stage only


def test_dockerfile_does_not_turn_authentication_off():
    dockerfile = _read("Dockerfile")
    for setting in ("AUTH_ENABLED", "API_KEYS", "DASHBOARD_AUTH_MODE", "DASHBOARD_AUTH_ENABLED"):
        assert setting not in dockerfile


def _active_lines(text: str) -> dict[str, str]:
    values = {}
    for line in text.splitlines():
        if line and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def test_env_example_uses_the_new_dashboard_setting():
    values = _active_lines(_read(".env.example"))
    assert values["DASHBOARD_AUTH_MODE"] == "off"
    assert "DASHBOARD_AUTH_ENABLED" not in values
    assert values["APP_ENV"] == "dev" and values["AUTH_ENABLED"] == "false"


def test_env_example_explains_demo_key_is_dev_only():
    text = _read(".env.example")
    assert "API_KEYS=demo-key:default:admin" in text
    assert "only when APP_ENV is dev or test" in text


def test_env_example_template_is_a_development_configuration(monkeypatch):
    _clear(monkeypatch)
    settings = Settings(_env_file=".env.example")
    assert settings.is_production_like is False and settings.env_api_keys_allowed is True
    assert startup_guard.startup_refusal_reasons(settings) == []
    assert settings.dashboard_auth_mode == "off" and settings.auth_enabled is False


def test_env_example_copied_to_production_is_refused(monkeypatch):
    """Changing only APP_ENV in the development template must not produce an open production API."""
    _clear(monkeypatch)
    monkeypatch.setenv("APP_ENV", "prod")
    settings = Settings(_env_file=".env.example")
    assert len(startup_guard.startup_refusal_reasons(settings)) == 2
    assert settings.env_api_keys_allowed is False


def test_deployment_guide_explains_docker_run():
    guide = _read("docs/deployment.md")
    assert "Running the Image Directly (`docker run`)" in guide
    assert "the image sets `APP_ENV=prod`" in guide
    assert "-e APP_ENV=dev" in guide and "refuses to start" in guide


def test_release_compose_comment_no_longer_claims_a_fallback_key():
    text = _read("deploy/docker-compose.release.example.yml")
    assert "since v1.6.3 environment keys" in text
