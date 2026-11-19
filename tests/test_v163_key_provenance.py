"""v1.6.3 P2 / P2.1: find database API keys that an environment API key created, or whose creator is unknown
(plan D6-D9, D20).

Covers the attribution of every event source (pre-v1.6.3 API events with the key id only in ``reason``, Dashboard
events, v1.6.3 API events with ``details.key_id``), ``unknown``, the ``env_created_api_keys`` operations check
(warn -> pass, never fail), the Dashboard "Created by" column and its permissions, tenant isolation, the
``python -m app.jobs.api_keys`` CLI and the agreement between ``list --created-by env`` and the check.

Every rule has negative cases: keys created on the Dashboard, by the CLI or with another database key are never
reported as environment-created, and a look-alike ``reason`` or another tenant's event never attributes a key.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.deps import get_event_publisher, get_state_store
from app.core.config import Settings, get_settings
from app.infra.postgres import get_session
from app.jobs import api_keys as api_keys_cli
from app.main import app
from app.models.audit_chain import AuditChainEntry
from app.models.governance_event import GovernanceEvent
from app.models.tenant import Tenant, TenantApiKey
from app.services import api_key_provenance as provenance
from app.services import operations
from app.services.auth import hash_api_key
from app.services.events import InMemoryEventPublisher
from app.services.governance_audit import add_governance_event
from app.services.run_state import InMemoryRunStateStore
from tests.auth_helpers import seed_db_api_key
from tests.openapi_contract import route_owners

ROOT = Path(__file__).resolve().parents[1]
ENV_KEY = "env-" + "provenance-key-v163"
DB_KEY = "rg_" + "v163-provenance-db-admin"
SETTINGS_KEYS = ["APP_ENV", "AUTH_ENABLED", "API_KEYS", "DASHBOARD_AUTH_MODE", "DASHBOARD_AUTH_ENABLED", "QUOTA_ENABLED"]


@pytest.fixture(autouse=True)
def _no_local_env_file(monkeypatch):
    # A developer's local .env must not decide these tests (CI has none).
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _env(monkeypatch, **env) -> None:
    for key in SETTINGS_KEYS:
        monkeypatch.delenv(key, raising=False)
    env.setdefault("QUOTA_ENABLED", "false")
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))
    get_settings.cache_clear()


def _dev_env(monkeypatch, **env) -> None:
    """dev, authentication on, the environment admin key accepted, Dashboard sign-in off (local admin)."""
    _env(monkeypatch, APP_ENV="dev", AUTH_ENABLED="true", API_KEYS=f"{ENV_KEY}:default:admin", DASHBOARD_AUTH_MODE="off", **env)


def _prod_env(monkeypatch, app_env: str = "prod") -> None:
    _env(monkeypatch, APP_ENV=app_env, AUTH_ENABLED="true", DASHBOARD_AUTH_MODE="session", API_KEYS="")


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


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# seed helpers: keys and the three event sources
# ---------------------------------------------------------------------------


def _key(key_id: str, tenant_id: str = "default", *, status: str = "active", role: str = "admin") -> dict:
    return {"key_id": key_id, "tenant_id": tenant_id, "status": status, "role": role}


def _api_event_v163(key_id: str, actor_key_id: str, tenant_id: str = "default") -> dict:
    """``POST /v1/tenants/{id}/api-keys`` from v1.6.3 on: ``details.key_id`` and ``actor_key_id``."""
    return {
        "tenant_id": tenant_id,
        "reason": f"API key {key_id} created.",
        "details": {"key_id": key_id, "role": "admin", "actor_key_id": actor_key_id},
    }


def _api_event_legacy(key_id: str, actor_key_id: str, tenant_id: str = "default") -> dict:
    """The same endpoint before v1.6.3: the key id only in ``reason``."""
    return {"tenant_id": tenant_id, "reason": f"API key {key_id} created.", "details": {"role": "admin", "actor_key_id": actor_key_id}}


def _dashboard_event(key_id: str, user_id: str, tenant_id: str = "default", name: str = "ops key") -> dict:
    return {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "reason": f"API key {name} (admin) issued for tenant {tenant_id}.",
        "details": {"key_id": key_id, "role": "admin", "key_prefix": "rg_abcde"},
    }


def _seed(session_factory, *, keys=(), events=()) -> None:
    async def scenario() -> None:
        async with session_factory() as session:
            tenants = {item["tenant_id"] for item in keys} | {item["tenant_id"] for item in events} | {"default"}
            for tenant_id in sorted(tenants):
                if await session.scalar(select(Tenant).where(Tenant.tenant_id == tenant_id)) is None:
                    session.add(Tenant(tenant_id=tenant_id, display_name=tenant_id, status="active"))
            for item in keys:
                session.add(
                    TenantApiKey(
                        key_id=item["key_id"],
                        tenant_id=item["tenant_id"],
                        name=f"name of {item['key_id']}",
                        key_prefix="rg_abcde",
                        key_hash=hash_api_key("secret-" + item["key_id"]),
                        role=item["role"],
                        status=item["status"],
                        revoked=item["status"] != "active",
                    )
                )
            for item in events:
                add_governance_event(
                    session,
                    tenant_id=item["tenant_id"],
                    user_id=item.get("user_id", ""),
                    event_type=item.get("event_type", provenance.API_KEY_CREATED),
                    decision="created",
                    reason=item["reason"],
                    details=item["details"],
                )
            await session.commit()

    _run(scenario())


def _creators(session_factory, tenant_id: str | None = None) -> dict[str, provenance.KeyCreator]:
    async def scenario():
        async with session_factory() as session:
            rows = await provenance.list_keys_with_creators(session, tenant_id=tenant_id)
            return {key.key_id: creator for key, creator in rows}

    return _run(scenario())


def _env_check(session_factory) -> operations.OperationsCheck:
    async def scenario():
        async with session_factory() as session:
            return await operations._env_created_api_keys_check(session)

    return _run(scenario())


# ---------------------------------------------------------------------------
# D6: attribution of every event source
# ---------------------------------------------------------------------------


def test_v163_api_event_with_details_key_id_from_an_environment_key_is_env(session_factory):
    _seed(session_factory, keys=[_key("key_new01")], events=[_api_event_v163("key_new01", "legacy")])
    creator = _creators(session_factory)["key_new01"]
    assert creator.kind == provenance.CREATOR_ENV and creator.label == provenance.ENV_LABEL and creator.actor == "key:legacy"


def test_v163_legacy_api_event_with_key_id_only_in_reason_is_env(session_factory):
    _seed(session_factory, keys=[_key("key_old01")], events=[_api_event_legacy("key_old01", "legacy")])
    assert _creators(session_factory)["key_old01"].kind == provenance.CREATOR_ENV


def test_v163_dashboard_event_is_the_signed_in_user(session_factory):
    _seed(session_factory, keys=[_key("key_dash01"), _key("key_dash02")],
          events=[_dashboard_event("key_dash01", "user:alice"), _dashboard_event("key_dash02", "dashboard-admin")])
    creators = _creators(session_factory)
    assert (creators["key_dash01"].kind, creators["key_dash01"].label) == (provenance.CREATOR_USER, "user:alice")
    assert (creators["key_dash02"].kind, creators["key_dash02"].label) == (provenance.CREATOR_USER, "dashboard-admin")


@pytest.mark.parametrize("factory", [_api_event_v163, _api_event_legacy])
def test_v163_api_event_from_a_database_key_is_that_key_not_env(session_factory, factory):
    _seed(session_factory, keys=[_key("key_child1")], events=[factory("key_child1", "key_parent1")])
    creator = _creators(session_factory)["key_child1"]
    assert (creator.kind, creator.label) == (provenance.CREATOR_KEY, "key:key_parent1")


def test_v163_key_without_event_is_unknown(session_factory):
    _seed(session_factory, keys=[_key("key_orphan")])
    creator = _creators(session_factory)["key_orphan"]
    assert creator.kind == provenance.CREATOR_UNKNOWN and creator.event_id is None and "no api_key.created" in creator.detail


def test_v163_event_without_any_actor_is_unknown_not_env(session_factory):
    """An API event written while AUTH_ENABLED=false records ``actor_key_id: ""``."""
    _seed(session_factory, keys=[_key("key_noauth")], events=[_api_event_v163("key_noauth", "")])
    creator = _creators(session_factory)["key_noauth"]
    assert creator.kind == provenance.CREATOR_UNKNOWN and creator.event_id is not None


@pytest.mark.parametrize(
    "reason",
    [
        "API key key_lookalike created. (copied)",
        "Note: API key key_lookalike created.",
        "API key key_lookalike (admin) issued for tenant default.",
        "API key key_lookalike revoked.",
    ],
)
def test_v163_reason_must_match_exactly(session_factory, reason):
    _seed(session_factory, keys=[_key("key_lookalike")],
          events=[{"tenant_id": "default", "reason": reason, "details": {"actor_key_id": "legacy"}}])
    assert _creators(session_factory)["key_lookalike"].kind == provenance.CREATOR_UNKNOWN


def test_v163_other_event_types_and_other_tenants_never_attribute_a_key(session_factory):
    _seed(
        session_factory,
        keys=[_key("key_t1only", "t1")],
        events=[
            {**_api_event_v163("key_t1only", "legacy", "t2")},  # another tenant's event naming this key
            {**_api_event_v163("key_t1only", "legacy", "t1"), "event_type": "api_key.revoked"},
        ],
    )
    assert _creators(session_factory)["key_t1only"].kind == provenance.CREATOR_UNKNOWN


def test_v163_earliest_event_wins_deterministically(session_factory):
    _seed(session_factory, keys=[_key("key_twice")],
          events=[_dashboard_event("key_twice", "user:bob"), _api_event_v163("key_twice", "legacy")])
    first = _creators(session_factory)["key_twice"]
    again = _creators(session_factory)["key_twice"]
    assert first == again and first.label == "user:bob"


def test_v163_attribute_keys_ignores_event_order():
    events = [
        GovernanceEvent(id=7, tenant_id="default", user_id="user:late", event_type="api_key.created", decision="created",
                        reason="x", details={"key_id": "key_order"}),
        GovernanceEvent(id=3, tenant_id="default", user_id="", event_type="api_key.created", decision="created",
                        reason="API key key_order created.", details={"actor_key_id": "legacy"}),
    ]
    key = TenantApiKey(key_id="key_order", tenant_id="default", name="n", key_prefix="p", key_hash="h", role="admin")
    assert provenance.attribute_keys([key], events)["key_order"].kind == provenance.CREATOR_ENV
    assert provenance.attribute_keys([key], list(reversed(events)))["key_order"].kind == provenance.CREATOR_ENV


def test_v163_unknown_created_by_filter_is_refused(session_factory):
    async def scenario():
        async with session_factory() as session:
            await provenance.list_keys_with_creators(session, created_by="demo")

    with pytest.raises(ValueError):
        _run(scenario())


# ---------------------------------------------------------------------------
# D6 end to end: the API event now carries details.key_id
# ---------------------------------------------------------------------------


def _readiness_checks(client: TestClient, key: str) -> dict[str, dict]:
    response = client.get("/v1/operations/readiness", headers={"X-API-Key": key})
    assert response.status_code == 200, response.text
    return {item["check_key"]: item for item in response.json()["checks"]}


def test_v163_api_created_key_with_env_key_warns_and_revoking_passes(monkeypatch, session_factory):
    _dev_env(monkeypatch)
    _seed(session_factory)
    with _client(session_factory) as client:
        assert _readiness_checks(client, ENV_KEY)["env_created_api_keys"]["status"] == "pass"
        created = client.post("/v1/tenants/default/api-keys", json={"name": "made-with-env", "role": "operator"},
                              headers={"X-API-Key": ENV_KEY})
        assert created.status_code == 200, created.text
        key_id = created.json()["key_id"]
        check = _readiness_checks(client, ENV_KEY)["env_created_api_keys"]
        assert check["status"] == "warn" and check["metadata"]["key_ids"] == [key_id] and key_id in check["detail"]
        revoked = client.post(f"/v1/tenants/default/api-keys/{key_id}/revoke", headers={"X-API-Key": ENV_KEY})
        assert revoked.status_code == 200
        assert _readiness_checks(client, ENV_KEY)["env_created_api_keys"]["status"] == "pass"

    async def event_details():
        async with session_factory() as session:
            event = await session.scalar(select(GovernanceEvent).where(GovernanceEvent.event_type == "api_key.created"))
            return event.details

    details = _run(event_details())
    assert details == {"key_id": key_id, "role": "operator", "actor_key_id": "legacy"}


def test_v163_api_created_key_with_a_database_key_is_not_reported(monkeypatch, session_factory):
    _prod_env(monkeypatch)
    parent = seed_db_api_key(session_factory, DB_KEY)
    with _client(session_factory) as client:
        created = client.post("/v1/tenants/default/api-keys", json={"name": "made-with-db", "role": "viewer"},
                              headers={"X-API-Key": DB_KEY})
        assert created.status_code == 200, created.text
        check = _readiness_checks(client, DB_KEY)["env_created_api_keys"]
    assert check["status"] == "pass" and check["metadata"]["count"] == 0
    assert _creators(session_factory)[created.json()["key_id"]].label == f"key:{parent}"


# ---------------------------------------------------------------------------
# D7: the operations check
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("app_env", ["prod", "staging", "dev"])
def test_v163_env_created_keys_warn_in_every_environment_and_never_fail(monkeypatch, session_factory, app_env):
    if app_env == "dev":
        _dev_env(monkeypatch)
        header_key = ENV_KEY
    else:
        _prod_env(monkeypatch, app_env)
        header_key = DB_KEY
        seed_db_api_key(session_factory, DB_KEY)
    _seed(session_factory, keys=[_key("key_envmade")], events=[_api_event_legacy("key_envmade", "legacy")])
    with _client(session_factory) as client:
        check = _readiness_checks(client, header_key)["env_created_api_keys"]
    assert check["status"] == "warn"
    assert check["metadata"]["key_ids"] == ["key_envmade"] and check["metadata"]["tenants"] == ["default"]


def test_v163_env_check_detail_explains_demo_key_cannot_be_told_and_points_to_runbook(session_factory):
    _seed(session_factory, keys=[_key("key_envmade")], events=[_api_event_v163("key_envmade", "legacy")])
    check = _env_check(session_factory)
    assert "cannot be told afterwards" in check.detail and "demo-key" in check.detail and "without API_KEYS" in check.detail
    assert "rotate" in check.detail and provenance.RUNBOOK_SECTION in check.detail
    assert check.metadata["runbook"] == provenance.RUNBOOK_SECTION


def test_v163_env_check_ignores_revoked_dashboard_cli_db_and_unknown_keys(session_factory):
    _seed(
        session_factory,
        keys=[
            _key("key_envrevoked", status="revoked"),
            _key("key_dashok"),
            _key("key_cliok"),
            _key("key_dbok"),
            _key("key_unknownok"),
        ],
        events=[
            _api_event_v163("key_envrevoked", "legacy"),
            _dashboard_event("key_dashok", "user:alice"),
            {"tenant_id": "default", "user_id": "cli", "reason": "API key key_cliok (admin) created from the CLI.",
             "details": {"key_id": "key_cliok", "role": "admin"}},
            _api_event_v163("key_dbok", "key_parent"),
        ],
    )
    check = _env_check(session_factory)
    assert check.status == "pass" and check.metadata["count"] == 0


def test_v163_env_check_lists_many_keys_with_a_cap(session_factory):
    ids = [f"key_env{n:02d}" for n in range(13)]
    _seed(session_factory, keys=[_key(key_id) for key_id in ids], events=[_api_event_v163(key_id, "legacy") for key_id in ids])
    check = _env_check(session_factory)
    assert check.status == "warn" and check.metadata["count"] == 13 and check.metadata["key_ids"] == sorted(ids)
    assert "and 3 more" in check.detail and "key_env12" not in check.detail


def test_v163_env_check_spans_tenants(session_factory):
    _seed(session_factory, keys=[_key("key_a1", "t1"), _key("key_b1", "t2")],
          events=[_api_event_legacy("key_a1", "legacy", "t1"), _api_event_v163("key_b1", "legacy", "t2")])
    check = _env_check(session_factory)
    assert check.metadata["key_ids"] == ["key_a1", "key_b1"] and check.metadata["tenants"] == ["t1", "t2"]


# ---------------------------------------------------------------------------
# D8: Dashboard "Created by" column, notice and permissions
# ---------------------------------------------------------------------------


def _creator_cells(html: str) -> list[str]:
    marker = 'data-testid="api-key-creator" data-creator-kind="'
    return [part.split('"', 1)[0] for part in html.split(marker)[1:]]


def test_v163_dashboard_shows_creator_column_badge_and_notice(monkeypatch, session_factory):
    _dev_env(monkeypatch)
    _seed(session_factory, keys=[_key("key_envmade"), _key("key_dashmade")],
          events=[_api_event_legacy("key_envmade", "legacy"), _dashboard_event("key_dashmade", "user:alice")])
    with _client(session_factory) as client:
        page = client.get("/dashboard/governance?tenant_id=default")
    assert page.status_code == 200
    html = page.text
    assert "Created by | 建立者" in html
    assert sorted(_creator_cells(html)) == ["env", "user"]
    assert provenance.ENV_LABEL in html and "<code>user:alice</code>" in html
    assert 'data-testid="env-created-keys-notice"' in html
    assert 'data-testid="env-created-keys-count">1<' in html and "key_envmade" in html
    assert provenance.RUNBOOK_URL in html


def test_v163_dashboard_without_env_created_keys_has_no_notice(monkeypatch, session_factory):
    _dev_env(monkeypatch)
    _seed(session_factory, keys=[_key("key_dashmade"), _key("key_orphan")], events=[_dashboard_event("key_dashmade", "user:alice")])
    with _client(session_factory) as client:
        html = client.get("/dashboard/governance?tenant_id=default").text
    assert "env-created-keys-notice" not in html
    assert sorted(_creator_cells(html)) == ["unknown", "user"]


def test_v163_dashboard_created_key_shows_the_dashboard_actor_and_revoking_clears_the_notice(monkeypatch, session_factory):
    _dev_env(monkeypatch)
    _seed(session_factory, keys=[_key("key_envmade")], events=[_api_event_v163("key_envmade", "legacy")])
    with _client(session_factory) as client:
        issued = client.post("/dashboard/governance/api-keys", data={"tenant_id": "default", "name": "replacement", "role": "operator"})
        assert issued.status_code == 200 and issued.headers["cache-control"] == "no-store"
        assert 'data-testid="env-created-keys-count">1<' in issued.text
        revoke = client.post("/dashboard/governance/tenants/default/api-keys/key_envmade/revoke", follow_redirects=False)
        assert revoke.status_code == 303
        html = client.get("/dashboard/governance?tenant_id=default").text
        assert "env-created-keys-notice" not in html
        checks = _readiness_checks(client, ENV_KEY)
    assert checks["env_created_api_keys"]["status"] == "pass"
    new_key = next(key for key, creator in _creators(session_factory).items() if key != "key_envmade")
    assert _creators(session_factory)[new_key].label == "dashboard-admin"


def test_v163_session_admin_sees_their_account_as_creator(client, signed_in, session_factory):
    who = signed_in(role="admin", username="key-admin")
    _seed(session_factory)
    issued = client.post("/dashboard/governance/api-keys", data={"tenant_id": "default", "name": "by-session", "role": "viewer"})
    assert issued.status_code == 200
    assert f"<code>user:{who.user.username}</code>" in issued.text
    assert _creator_cells(issued.text) == ["user"]


def test_v163_auditor_sees_creators_and_notice_but_cannot_revoke(client, signed_in, session_factory):
    _seed(session_factory, keys=[_key("key_envmade")], events=[_api_event_v163("key_envmade", "legacy")])
    signed_in(role="auditor")
    page = client.get("/dashboard/governance?tenant_id=default")
    assert page.status_code == 200
    assert _creator_cells(page.text) == ["env"] and 'data-testid="env-created-keys-notice"' in page.text
    assert "/revoke" not in page.text and "Revoke Key" not in page.text
    refused = client.post("/dashboard/governance/tenants/default/api-keys/key_envmade/revoke", follow_redirects=False)
    assert refused.status_code == 403
    assert _env_check(session_factory).status == "warn"


def test_v163_tenant_bound_auditor_only_sees_their_tenant(client, signed_in, session_factory):
    _seed(
        session_factory,
        keys=[_key("key_t1env", "t1"), _key("key_t2env", "t2")],
        events=[_api_event_v163("key_t1env", "legacy", "t1"), _api_event_v163("key_t2env", "legacy", "t2")],
    )
    signed_in(role="auditor", tenant_id="t1")
    page = client.get("/dashboard/governance")
    assert page.status_code == 200
    assert "key_t1env" in page.text and "key_t2env" not in page.text
    assert 'data-testid="env-created-keys-count">1<' in page.text
    assert client.get("/dashboard/governance?tenant_id=t2").status_code == 403


def test_v163_tenant_admin_sees_no_keys_and_no_notice(client, signed_in, session_factory):
    _seed(session_factory, keys=[_key("key_t1env", "t1")], events=[_api_event_v163("key_t1env", "legacy", "t1")])
    signed_in(role="tenant_admin", tenant_id="t1")
    page = client.get("/dashboard/governance")
    assert page.status_code == 200
    # The key table stays empty (no ``api_keys.view``); the tenant's own governance events are listed as before.
    assert "name of key_t1env" not in page.text and "env-created-keys-notice" not in page.text and _creator_cells(page.text) == []


def test_v163_governance_dashboard_routes_moved_to_their_router():
    governance_paths = {
        "/dashboard/governance",
        "/dashboard/governance/tenants",
        "/dashboard/governance/api-keys",
        "/dashboard/governance/tenants/{tenant_id}/api-keys",
        "/dashboard/governance/tenants/{tenant_id}/api-keys/{key_id}/revoke",
    }
    # route_owners walks included routers too (newer FastAPI keeps them as _IncludedRouter entries in app.routes).
    modules = {path: module for (path, _method), module in route_owners(app).items() if path in governance_paths}
    assert modules == {path: "app.ui.routers.governance" for path in governance_paths}
    ui_routes = (ROOT / "app" / "ui" / "routes.py").read_text(encoding="utf-8")
    assert '"/dashboard/governance' not in ui_routes


# ---------------------------------------------------------------------------
# D9: the API key CLI
# ---------------------------------------------------------------------------


def _cli(capsys, session_factory, *argv: str) -> tuple[int, dict, str]:
    code = api_keys_cli.main(list(argv), settings=get_settings(), session_factory=session_factory)
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.strip()]
    assert len(lines) == 1, out
    return code, json.loads(lines[0]), out


def test_v163_cli_create_prints_the_plaintext_once_and_stores_only_the_hash(capsys, session_factory):
    _seed(session_factory)
    code, payload, out = _cli(capsys, session_factory, "create", "--tenant", "default", "--role", "admin", "--name", "first-key")
    assert code == 0 and payload["action"] == "create"
    plaintext = payload["plaintext_key"]
    assert out.count(plaintext) == 1 and "only once" in payload["notice"]
    key_id = payload["api_key"]["key_id"]
    assert payload["api_key"]["created_by"]["kind"] == provenance.CREATOR_CLI
    assert "key_hash" not in payload["api_key"]

    async def stored():
        async with session_factory() as session:
            key = await session.scalar(select(TenantApiKey).where(TenantApiKey.key_id == key_id))
            events = (await session.scalars(select(GovernanceEvent).where(GovernanceEvent.event_type == "api_key.created"))).all()
            chain = (await session.scalars(select(AuditChainEntry).where(AuditChainEntry.ref_table == "governance_events"))).all()
            return key, events, chain

    key, events, chain = _run(stored())
    assert key.key_hash == hash_api_key(plaintext) and key.status == "active" and key.role == "admin"
    assert len(events) == 1 and events[0].user_id == "cli" and events[0].details["key_id"] == key_id
    assert plaintext not in json.dumps(events[0].details) and plaintext not in events[0].reason
    assert str(events[0].id) in {entry.ref_id for entry in chain}
    _, listed, listed_out = _cli(capsys, session_factory, "list")
    assert plaintext not in listed_out and listed["api_keys"][0]["key_id"] == key_id


def test_v163_cli_created_key_authenticates_in_production_and_is_not_env_created(monkeypatch, capsys, session_factory):
    _seed(session_factory)
    _, payload, _ = _cli(capsys, session_factory, "create", "--tenant", "default", "--role", "admin", "--name", "headless")
    _prod_env(monkeypatch)
    with _client(session_factory) as client:
        listed = client.get("/v1/tenants/default/api-keys", headers={"X-API-Key": payload["plaintext_key"]})
        assert listed.status_code == 200
        assert _readiness_checks(client, payload["plaintext_key"])["env_created_api_keys"]["status"] == "pass"


@pytest.mark.parametrize(
    ("argv", "code"),
    [
        (["--tenant", "missing", "--role", "admin", "--name", "x"], "tenant_not_found"),
        (["--tenant", "paused", "--role", "admin", "--name", "x"], "tenant_inactive"),
        (["--tenant", "default", "--role", "admin", "--name", "   "], "invalid_name"),
        (["--tenant", "default", "--role", "admin", "--name", "n" * 129], "invalid_name"),
    ],
)
def test_v163_cli_create_refusals_write_nothing(capsys, session_factory, argv, code):
    _seed(session_factory)

    async def pause():
        async with session_factory() as session:
            session.add(Tenant(tenant_id="paused", display_name="paused", status="disabled"))
            await session.commit()

    _run(pause())
    exit_code, payload, _ = _cli(capsys, session_factory, "create", *argv)
    assert exit_code == 2 and payload["error"]["code"] == code and "plaintext_key" not in payload

    async def counts():
        async with session_factory() as session:
            return len((await session.scalars(select(TenantApiKey))).all()), len((await session.scalars(select(GovernanceEvent))).all())

    assert _run(counts()) == (0, 0)


def test_v163_cli_rejects_unknown_roles_and_has_no_plaintext_argument():
    with pytest.raises(SystemExit):
        api_keys_cli.build_parser().parse_args(["create", "--tenant", "default", "--role", "root", "--name", "x"])
    assert set(api_keys_cli.API_KEY_ROLES) >= {"admin", "auditor", "viewer"}
    source = inspect.getsource(api_keys_cli.build_parser)
    assert "--key" not in source and "--plaintext" not in source


def test_v163_cli_list_created_by_env_matches_the_operations_check(capsys, session_factory):
    _seed(
        session_factory,
        keys=[
            _key("key_env1", "t1"),
            _key("key_env2", "t2"),
            _key("key_envrevoked", status="revoked"),
            _key("key_dash"),
            _key("key_orphan"),
        ],
        events=[
            _api_event_legacy("key_env1", "legacy", "t1"),
            _api_event_v163("key_env2", "legacy", "t2"),
            _api_event_v163("key_envrevoked", "legacy"),
            _dashboard_event("key_dash", "user:alice"),
        ],
    )
    _, payload, _ = _cli(capsys, session_factory, "list", "--created-by", "env")
    listed = sorted(item["key_id"] for item in payload["api_keys"])
    check = _env_check(session_factory)
    assert listed == check.metadata["key_ids"] == ["key_env1", "key_env2"] and payload["count"] == 2
    assert all(item["created_by"]["kind"] == "env" for item in payload["api_keys"])

    _, with_revoked, _ = _cli(capsys, session_factory, "list", "--created-by", "env", "--include-revoked")
    assert sorted(item["key_id"] for item in with_revoked["api_keys"]) == ["key_env1", "key_env2", "key_envrevoked"]
    _, tenant_only, _ = _cli(capsys, session_factory, "list", "--created-by", "env", "--tenant", "t1")
    assert [item["key_id"] for item in tenant_only["api_keys"]] == ["key_env1"]
    _, unknown, _ = _cli(capsys, session_factory, "list", "--created-by", "unknown")
    assert [item["key_id"] for item in unknown["api_keys"]] == ["key_orphan"]


# ---------------------------------------------------------------------------
# D20 (P2.1): keys whose creator is unknown get their own check
# ---------------------------------------------------------------------------


def _unattributed_check(session_factory) -> operations.OperationsCheck:
    async def scenario():
        async with session_factory() as session:
            return await operations._unattributed_api_keys_check(session)

    return _run(scenario())


def test_v163_unknown_creators_carry_a_reason_and_other_kinds_none(session_factory):
    _seed(
        session_factory,
        keys=[_key("key_noevent"), _key("key_noactor"), _key("key_envone"), _key("key_dashone")],
        events=[_api_event_v163("key_noactor", ""), _api_event_v163("key_envone", "legacy"), _dashboard_event("key_dashone", "user:a")],
    )
    creators = _creators(session_factory)
    assert creators["key_noevent"].reason == provenance.UNKNOWN_NO_EVENT_REASON
    assert creators["key_noactor"].reason == provenance.UNKNOWN_NO_ACTOR_REASON
    assert creators["key_envone"].reason == "" and creators["key_dashone"].reason == ""
    assert creators["key_noactor"].as_dict()["reason"] == "no_actor"


def test_v163_unattributed_check_warns_and_splits_the_reasons(session_factory):
    _seed(session_factory, keys=[_key("key_noevent"), _key("key_noactor", "t2")], events=[_api_event_legacy("key_noactor", "", "t2")])
    check = _unattributed_check(session_factory)
    assert check.check_key == "unattributed_api_keys" and check.status == "warn"
    assert check.metadata["key_ids"] == ["key_noactor", "key_noevent"] and check.metadata["count"] == 2
    assert check.metadata["no_actor"] == ["key_noactor"] and check.metadata["no_event"] == ["key_noevent"]
    assert check.metadata["tenants"] == ["default", "t2"]
    assert "AUTH_ENABLED=false" in check.detail and "outside the application" in check.detail
    assert provenance.RUNBOOK_SECTION in check.detail
    # The environment-key check is a different question and stays quiet.
    assert _env_check(session_factory).status == "pass"


def test_v163_unattributed_check_ignores_attributed_and_revoked_keys(session_factory):
    _seed(
        session_factory,
        keys=[_key("key_envmade"), _key("key_dashok"), _key("key_cliok"), _key("key_dbok"), _key("key_gone", status="revoked")],
        events=[
            _api_event_v163("key_envmade", "legacy"),
            _dashboard_event("key_dashok", "dashboard-admin"),
            {"tenant_id": "default", "user_id": "cli", "reason": "API key key_cliok (admin) created from the CLI.",
             "details": {"key_id": "key_cliok", "role": "admin"}},
            _api_event_v163("key_dbok", "key_parent"),
        ],
    )
    check = _unattributed_check(session_factory)
    assert check.status == "pass" and check.metadata["count"] == 0 and "recorded creator" in check.detail


def test_v163_unattributed_check_caps_the_listed_ids(session_factory):
    ids = [f"key_orph{n:02d}" for n in range(12)]
    _seed(session_factory, keys=[_key(key_id) for key_id in ids])
    check = _unattributed_check(session_factory)
    assert check.metadata["count"] == 12 and "and 2 more" in check.detail and "key_orph11" not in check.detail


def test_v163_key_created_through_the_api_with_auth_off_warns_until_revoked(monkeypatch, session_factory):
    _env(monkeypatch, APP_ENV="dev", AUTH_ENABLED="false", DASHBOARD_AUTH_MODE="off")
    _seed(session_factory)
    with _client(session_factory) as client:
        created = client.post("/v1/tenants/default/api-keys", json={"name": "made-without-auth", "role": "admin"})
        assert created.status_code == 200, created.text
        key_id = created.json()["key_id"]
        checks = {item["check_key"]: item for item in client.get("/v1/operations/readiness").json()["checks"]}
        assert checks["unattributed_api_keys"]["status"] == "warn"
        assert checks["unattributed_api_keys"]["metadata"]["no_actor"] == [key_id]
        assert checks["env_created_api_keys"]["status"] == "pass"
        assert client.post(f"/v1/tenants/default/api-keys/{key_id}/revoke").status_code == 200
        checks = {item["check_key"]: item for item in client.get("/v1/operations/readiness").json()["checks"]}
    assert checks["unattributed_api_keys"]["status"] == "pass"


@pytest.mark.parametrize("app_env", ["prod", "staging"])
def test_v163_unattributed_keys_warn_in_production_and_never_fail(monkeypatch, session_factory, app_env):
    _prod_env(monkeypatch, app_env)
    seed_db_api_key(session_factory, DB_KEY)  # no creation event either: it is unattributed too
    _seed(session_factory, keys=[_key("key_noactor")], events=[_api_event_v163("key_noactor", "")])
    with _client(session_factory) as client:
        check = _readiness_checks(client, DB_KEY)["unattributed_api_keys"]
    assert check["status"] == "warn" and "key_noactor" in check["metadata"]["no_actor"]


def test_v163_dashboard_shows_the_unattributed_notice(monkeypatch, session_factory):
    _dev_env(monkeypatch)
    _seed(session_factory, keys=[_key("key_noactor"), _key("key_dashmade")],
          events=[_api_event_v163("key_noactor", ""), _dashboard_event("key_dashmade", "user:alice")])
    with _client(session_factory) as client:
        html = client.get("/dashboard/governance?tenant_id=default").text
    assert 'data-testid="unattributed-keys-notice"' in html and 'data-testid="unattributed-keys-count">1<' in html
    assert "key_noactor" in html.split('data-testid="unattributed-keys-ids">', 1)[1].split("<", 1)[0]
    assert "env-created-keys-notice" not in html


def test_v163_dashboard_without_unattributed_keys_has_no_second_notice(monkeypatch, session_factory):
    _dev_env(monkeypatch)
    _seed(session_factory, keys=[_key("key_envmade")], events=[_api_event_v163("key_envmade", "legacy")])
    with _client(session_factory) as client:
        html = client.get("/dashboard/governance?tenant_id=default").text
    assert "unattributed-keys-notice" not in html and 'data-testid="env-created-keys-notice"' in html


def test_v163_tenant_bound_auditor_counts_only_their_unattributed_keys(client, signed_in, session_factory):
    _seed(session_factory, keys=[_key("key_t1orph", "t1"), _key("key_t2orph", "t2")])
    signed_in(role="auditor", tenant_id="t1")
    page = client.get("/dashboard/governance")
    assert page.status_code == 200
    assert 'data-testid="unattributed-keys-count">1<' in page.text and "key_t2orph" not in page.text


def test_v163_cli_list_unknown_matches_the_unattributed_check(capsys, session_factory):
    _seed(
        session_factory,
        keys=[_key("key_noevent"), _key("key_noactor"), _key("key_envmade"), _key("key_gone", status="revoked")],
        events=[_api_event_v163("key_noactor", ""), _api_event_v163("key_envmade", "legacy")],
    )
    _, payload, _ = _cli(capsys, session_factory, "list", "--created-by", "unknown")
    listed = sorted(item["key_id"] for item in payload["api_keys"])
    assert listed == _unattributed_check(session_factory).metadata["key_ids"] == ["key_noactor", "key_noevent"]
    reasons = {item["key_id"]: item["created_by"]["reason"] for item in payload["api_keys"]}
    assert reasons == {"key_noactor": "no_actor", "key_noevent": "no_event"}
