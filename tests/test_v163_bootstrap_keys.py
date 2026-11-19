"""v1.6.3 P1: no built-in environment API key, environment keys refused outside dev / test, startup refusal for
AUTH_ENABLED=false in production, startup notices and the Operations checks (plan D1-D5, D16).

Every rule has negative cases: dev / test environment keys keep working, database keys keep working in production,
and a development API with authentication off still starts.
"""

from __future__ import annotations

import io
import logging
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_event_publisher, get_state_store
from app.core.config import Settings, get_settings
from app.infra.postgres import get_session
from app.main import app
from app.services import operations, startup_guard
from app.services.events import InMemoryEventPublisher
from app.services.run_state import InMemoryRunStateStore
from tests.auth_helpers import seed_db_api_key

ENV_KEY = "env-" + "bootstrap-key-v163"
DB_KEY = "rg_" + "v163-db-admin-key"
DEMO_KEY = "demo" + "-key"
PRODUCTION_LIKE = ["prod", "production", "staging", "", "  PROD  ", "qa"]
DEVELOPMENT = ["dev", "test", "DEV", " Test "]
SETTINGS_KEYS = ["APP_ENV", "AUTH_ENABLED", "API_KEYS", "DASHBOARD_AUTH_MODE", "DASHBOARD_AUTH_ENABLED", "QUOTA_ENABLED"]


def _env(monkeypatch, **env) -> None:
    for key in SETTINGS_KEYS:
        monkeypatch.delenv(key, raising=False)
    # A developer's local .env must not decide these tests (CI has none).
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    env.setdefault("QUOTA_ENABLED", "false")
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))
    get_settings.cache_clear()


def _prod_env(monkeypatch, app_env: str = "prod", **env) -> None:
    # A production-like API needs authentication on and Dashboard sign-in (D4, D17) to start at all.
    _env(monkeypatch, APP_ENV=app_env, AUTH_ENABLED="true", DASHBOARD_AUTH_MODE="session", **env)


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


def _metrics(client: TestClient, key: str | None):
    return client.get("/v1/metrics", headers={"X-API-Key": key} if key is not None else {})


# ---------------------------------------------------------------------------
# D1 / D16: defaults
# ---------------------------------------------------------------------------


def test_program_defaults_have_no_environment_key_and_require_auth(monkeypatch):
    for key in SETTINGS_KEYS:
        monkeypatch.delenv(key, raising=False)
    settings = Settings(_env_file=None)
    assert settings.api_keys == ""
    assert settings.auth_enabled is True
    assert startup_guard.usable_env_api_key_count(settings) == 0


@pytest.mark.parametrize("app_env", ["dev", "prod"])
def test_demo_key_is_rejected_when_api_keys_is_unset(monkeypatch, session_factory, app_env):
    _env(monkeypatch, APP_ENV=app_env, AUTH_ENABLED="true", DASHBOARD_AUTH_MODE="session")
    with _client(session_factory) as client:
        response = _metrics(client, DEMO_KEY)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"


# ---------------------------------------------------------------------------
# D2 / D3: environment keys only in dev / test
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("app_env", DEVELOPMENT)
def test_env_api_keys_allowed_in_dev_and_test(app_env):
    settings = Settings(_env_file=None, app_env=app_env)
    assert settings.env_api_keys_allowed is True
    assert settings.is_production_like is False


@pytest.mark.parametrize("app_env", PRODUCTION_LIKE)
def test_env_api_keys_refused_everywhere_else(app_env):
    settings = Settings(_env_file=None, app_env=app_env)
    assert settings.env_api_keys_allowed is False
    assert settings.is_production_like is True


@pytest.mark.parametrize("app_env", ["prod", "production", "staging", ""])
def test_refused_env_key_answers_exactly_like_a_wrong_key(monkeypatch, session_factory, app_env):
    _prod_env(monkeypatch, app_env, API_KEYS=f"{ENV_KEY}:default:admin")
    with _client(session_factory) as client:
        refused = _metrics(client, ENV_KEY)
        wrong = _metrics(client, "not-a-configured-key")
    assert refused.status_code == wrong.status_code == 401
    assert refused.json() == wrong.json() == {"error": {"code": "invalid_api_key", "message": "X-API-Key is invalid."}}
    assert refused.headers.get("www-authenticate") == wrong.headers.get("www-authenticate")


@pytest.mark.parametrize("app_env", ["dev", "test", "DEV"])
def test_env_key_still_works_in_dev_and_test(monkeypatch, session_factory, app_env):
    _env(monkeypatch, APP_ENV=app_env, AUTH_ENABLED="true", API_KEYS=f"{ENV_KEY}:default:admin")
    with _client(session_factory) as client:
        assert _metrics(client, ENV_KEY).status_code == 200
        assert _metrics(client, None).status_code == 401


def test_database_key_keeps_working_in_production(monkeypatch, session_factory):
    _prod_env(monkeypatch, API_KEYS=f"{ENV_KEY}:default:admin")
    seed_db_api_key(session_factory, DB_KEY)
    with _client(session_factory) as client:
        assert _metrics(client, DB_KEY).status_code == 200
        assert _metrics(client, ENV_KEY).status_code == 401


def test_tenant_status_still_applies_to_database_keys_and_refused_env_keys_reveal_nothing(monkeypatch, session_factory):
    _prod_env(monkeypatch, API_KEYS=f"{ENV_KEY}:paused:admin")
    seed_db_api_key(session_factory, DB_KEY, tenant_id="paused", tenant_status="suspended")
    with _client(session_factory) as client:
        db = _metrics(client, DB_KEY)
        env = _metrics(client, ENV_KEY)
    assert db.status_code == 403 and db.json()["error"]["code"] == "tenant_inactive"
    assert env.status_code == 401 and env.json()["error"]["code"] == "invalid_api_key"


def test_auth_disabled_in_dev_needs_no_key(monkeypatch, session_factory):
    _env(monkeypatch, APP_ENV="dev", AUTH_ENABLED="false")
    with _client(session_factory) as client:
        assert _metrics(client, None).status_code == 200


# ---------------------------------------------------------------------------
# D4: the API refuses to start with AUTH_ENABLED=false outside dev / test
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("app_env", ["prod", "production", "staging", ""])
def test_api_refuses_to_start_with_auth_disabled_in_production(monkeypatch, session_factory, app_env):
    _env(monkeypatch, APP_ENV=app_env, AUTH_ENABLED="false", DASHBOARD_AUTH_MODE="session")
    with pytest.raises(startup_guard.InsecureStartupConfiguration) as excinfo:
        with _client(session_factory):
            pass
    message = str(excinfo.value)
    assert "AUTH_ENABLED=true" in message and "APP_ENV=dev" in message
    assert len(excinfo.value.reasons) == 1


@pytest.mark.parametrize("app_env", ["dev", "test"])
def test_api_starts_with_auth_disabled_in_dev_and_test(monkeypatch, session_factory, app_env):
    _env(monkeypatch, APP_ENV=app_env, AUTH_ENABLED="false", DASHBOARD_AUTH_MODE="off")
    with _client(session_factory) as client:
        assert client.get("/v1/healthz").status_code == 200


def test_settings_load_and_cli_runs_with_insecure_production_settings(monkeypatch, session_factory):
    """D3 / D4: only the API lifespan refuses; Settings, the worker and app.jobs.* keep working (no exception)."""
    from app.jobs import users

    settings = Settings(_env_file=None, app_env="prod", auth_enabled=False, dashboard_auth_mode="off")
    assert startup_guard.startup_refusal_reasons(settings)  # the API would refuse ...
    stdout = io.StringIO()
    monkeypatch.setattr("sys.stdout", stdout)
    assert users.main(["list"], settings=settings, session_factory=session_factory) == 0  # ... the CLI does not
    assert '"users"' in stdout.getvalue() or "[]" in stdout.getvalue()


def test_only_the_api_lifespan_uses_the_startup_guard():
    from pathlib import Path

    users_of_guard = sorted(
        str(path).replace("\\", "/")
        for path in Path("app").rglob("*.py")
        if "startup_guard" in path.read_text(encoding="utf-8") and path.name != "startup_guard.py"
    )
    assert users_of_guard == ["app/main.py"]


# ---------------------------------------------------------------------------
# D2 / D16: startup notices (never the key itself)
# ---------------------------------------------------------------------------


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def test_refused_env_keys_are_logged_as_a_count_only(caplog):
    settings = Settings(_env_file=None, app_env="prod", api_keys=f"{ENV_KEY}:default:admin,second-key:t2:viewer")
    with caplog.at_level(logging.WARNING, logger="app.services.startup_guard"):
        notices = _run(startup_guard.report_api_key_status(settings, None, check_database=False))
    assert notices and notices[0].startswith("2 environment API key(s)")
    assert ENV_KEY not in caplog.text and "second-key" not in caplog.text
    assert "2 environment API key(s)" in caplog.text


def test_startup_lifespan_never_logs_the_key(monkeypatch, session_factory, caplog):
    _prod_env(monkeypatch, API_KEYS=f"{ENV_KEY}:default:admin")
    with caplog.at_level(logging.DEBUG):
        with _client(session_factory):
            pass
    assert "1 environment API key(s)" in caplog.text
    assert ENV_KEY not in caplog.text


@pytest.mark.parametrize("app_env", ["dev", "test"])
def test_dev_env_keys_produce_no_refusal_notice(app_env):
    settings = Settings(_env_file=None, app_env=app_env, api_keys=f"{ENV_KEY}:default:admin")
    assert startup_guard.refused_env_api_key_count(settings) == 0
    assert _run(startup_guard.report_api_key_status(settings, None, check_database=True)) == []


def test_no_api_key_notice_when_auth_is_on_and_nothing_is_available(session_factory):
    settings = Settings(_env_file=None, app_env="prod", auth_enabled=True, api_keys="")
    notices = _run(startup_guard.report_api_key_status(settings, session_factory, check_database=True))
    assert len(notices) == 1 and notices[0].startswith("No API key is available")


def test_no_api_key_notice_is_silent_with_an_active_database_key(session_factory):
    seed_db_api_key(session_factory, DB_KEY)
    settings = Settings(_env_file=None, app_env="prod", auth_enabled=True, api_keys="")
    assert _run(startup_guard.report_api_key_status(settings, session_factory, check_database=True)) == []


def test_no_api_key_notice_counts_only_active_unrevoked_keys(session_factory):
    from sqlalchemy import update

    from app.models.tenant import TenantApiKey

    seed_db_api_key(session_factory, DB_KEY)

    async def revoke():
        async with session_factory() as session:
            await session.execute(update(TenantApiKey).values(revoked=True, status="revoked"))
            await session.commit()

    _run(revoke())
    settings = Settings(_env_file=None, app_env="prod", auth_enabled=True, api_keys="")
    notices = _run(startup_guard.report_api_key_status(settings, session_factory, check_database=True))
    assert len(notices) == 1 and notices[0].startswith("No API key is available")


@pytest.mark.parametrize(
    "values",
    [
        {"app_env": "dev", "auth_enabled": False, "api_keys": ""},  # authentication off: nothing to warn about
        {"app_env": "dev", "auth_enabled": True, "api_keys": f"{ENV_KEY}:default:admin"},  # a usable dev key
    ],
)
def test_no_api_key_notice_negative_cases(session_factory, values):
    settings = Settings(_env_file=None, **values)
    assert _run(startup_guard.report_api_key_status(settings, session_factory, check_database=True)) == []


def test_no_api_key_notice_never_blocks_when_the_database_is_unreachable():
    class Unreachable:
        def __call__(self):
            raise OSError("connection refused")

    settings = Settings(_env_file=None, app_env="prod", auth_enabled=True, api_keys="")
    assert _run(startup_guard.report_api_key_status(settings, Unreachable(), check_database=True)) == []


# ---------------------------------------------------------------------------
# D4 / D5: Operations checks
# ---------------------------------------------------------------------------


def _readiness_checks(client: TestClient, key: str) -> dict[str, dict]:
    response = client.get("/v1/operations/readiness", headers={"X-API-Key": key})
    assert response.status_code == 200, response.text
    return {item["check_key"]: item for item in response.json()["checks"]}


@pytest.mark.parametrize("app_env", ["prod", "production", "staging"])
def test_bootstrap_keys_check_covers_every_production_like_env(monkeypatch, session_factory, app_env):
    _prod_env(monkeypatch, app_env, API_KEYS=f"{ENV_KEY}:default:admin")
    seed_db_api_key(session_factory, DB_KEY)
    with _client(session_factory) as client:
        checks = _readiness_checks(client, DB_KEY)
    bootstrap = checks["bootstrap_keys_in_prod"]
    assert bootstrap["status"] == "fail" and "refused" in bootstrap["detail"]
    assert ENV_KEY not in bootstrap["detail"] and ENV_KEY not in str(bootstrap["metadata"])
    assert checks["api_auth_enabled"]["status"] == "pass"


def test_bootstrap_keys_check_passes_in_production_without_api_keys(monkeypatch, session_factory):
    _prod_env(monkeypatch, API_KEYS="")
    seed_db_api_key(session_factory, DB_KEY)
    with _client(session_factory) as client:
        checks = _readiness_checks(client, DB_KEY)
    assert checks["bootstrap_keys_in_prod"]["status"] == "pass"


def test_bootstrap_keys_check_is_absent_in_dev(monkeypatch, session_factory):
    _env(monkeypatch, APP_ENV="dev", AUTH_ENABLED="true", API_KEYS=f"{ENV_KEY}:default:admin")
    with _client(session_factory) as client:
        checks = _readiness_checks(client, ENV_KEY)
    assert "bootstrap_keys_in_prod" not in checks
    assert checks["api_auth_enabled"]["status"] == "pass"


@pytest.mark.parametrize(
    ("values", "status"),
    [
        ({"app_env": "prod", "auth_enabled": False}, "fail"),
        ({"app_env": "staging", "auth_enabled": False}, "fail"),
        ({"app_env": "prod", "auth_enabled": True}, "pass"),
        ({"app_env": "dev", "auth_enabled": False}, "pass"),
        ({"app_env": "test", "auth_enabled": False}, "pass"),
    ],
)
def test_api_auth_enabled_check(values, status):
    check = operations._api_auth_enabled_check(Settings(_env_file=None, **values))
    assert check.check_key == "api_auth_enabled" and check.status == status
    assert check.metadata["production_like"] is (values["app_env"] not in {"dev", "test"})
