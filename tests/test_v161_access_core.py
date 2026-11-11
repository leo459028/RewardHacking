"""v1.6.1 P1: migration, models and the access core services (plan D11-D22, D46, D47).

Service-level tests on in-memory SQLite; nothing here touches a route (P1 changes no behaviour). Passwords are
generated at run time (never a literal) and scrypt runs with a cheap cost so the suite stays fast.
"""

from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select

from app.core import digests
from app.core.config import Settings
from app.jobs import users as users_cli
from app.models.access import ChangeRequest, DashboardSession, DashboardUser
from app.models.audit_chain import AuditChainCheckpoint, AuditChainEntry
from app.models.base import Base
from app.models.governance_event import GovernanceEvent
from app.services import retention
from app.services.access import passwords, permissions, providers, sessions, users
from app.services.access.errors import AccessError
from app.services.access.principal import LOCAL_ADMIN, Identity, principal_for

MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "202611120003_add_v161_access_audit_integrity.py"
NEW_TABLES = ("dashboard_users", "dashboard_sessions", "change_requests", "audit_chain_entries", "audit_chain_checkpoints")
CHEAP = {"dashboard_password_scrypt_n": 16, "dashboard_password_scrypt_r": 1, "dashboard_password_scrypt_p": 1}
CHEAP_PARAMS = passwords.ScryptParams(n=16, r=1, p=1)


def _settings(**values) -> Settings:
    return Settings(_env_file=None, **{**CHEAP, **values})


def _pw() -> str:
    return "pw-" + secrets.token_hex(8)  # 19 characters, unique per call


def _db(session_factory, fn):
    async def scenario():
        async with session_factory() as session:
            return await fn(session)

    return asyncio.run(scenario())


async def _create(session, settings, username="alice", role="admin", tenant_id=None, password=None, commit=True):
    user = await users.create_user(
        session, settings, username=username, password=password or _pw(), role=role, tenant_id=tenant_id, actor="test"
    )
    if commit:
        await session.commit()
    return user


def _load_migration():
    spec = importlib.util.spec_from_file_location(MIGRATION.stem, MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# migration and models (D46)
# ---------------------------------------------------------------------------


def test_migration_revision_follows_head_and_rule():
    module = _load_migration()
    assert module.revision == "202611120003"
    assert module.down_revision == "202611120002"
    assert "Create Date: 2026-11-11" in module.__doc__


def test_migration_upgrade_matches_models_and_downgrade_drops_tables():
    module = _load_migration()
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            module.upgrade()
        inspector = sa.inspect(conn)
        for table in NEW_TABLES:
            migrated = {column["name"] for column in inspector.get_columns(table)}
            assert migrated == set(Base.metadata.tables[table].columns.keys()), table
        with Operations.context(MigrationContext.configure(conn)):
            module.downgrade()
        assert not set(NEW_TABLES) & set(sa.inspect(conn).get_table_names())
    engine.dispose()


def test_new_models_register_tables_and_retention_never_deletes_them():
    for model in (DashboardUser, DashboardSession, ChangeRequest, AuditChainEntry, AuditChainCheckpoint):
        assert model.__tablename__ in Base.metadata.tables
    assert not set(retention.DELETED_TABLES) & set(NEW_TABLES)


def test_digests_moved_without_changing_output():
    assert retention.canonical_json is digests.canonical_json and retention.sha256_hex is digests.sha256_hex
    payload = {"b": 1, "a": datetime(2026, 1, 1, 12, 0), "c": "令牌"}
    assert digests.canonical_json(payload) == '{"a":"2026-01-01T12:00:00+00:00","b":1,"c":"令牌"}'
    assert digests.sha256_hex("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


# ---------------------------------------------------------------------------
# settings (D9, D47)
# ---------------------------------------------------------------------------


def test_auth_mode_is_derived_from_the_old_flag_when_unset(monkeypatch):
    monkeypatch.delenv("DASHBOARD_AUTH_MODE", raising=False)  # tests/conftest.py pins it to off
    assert Settings(_env_file=None).dashboard_auth_mode == "off"
    assert Settings(_env_file=None, dashboard_auth_enabled=True).dashboard_auth_mode == "session"
    assert Settings(_env_file=None, dashboard_auth_enabled=True, dashboard_auth_mode=" Off ").dashboard_auth_mode == "off"
    assert Settings(_env_file=None, dashboard_auth_mode="SESSION").dashboard_auth_mode == "session"


@pytest.mark.parametrize(
    "values",
    [
        {"dashboard_auth_mode": "basic"},
        {"dashboard_cookie_secure": "maybe"},
        {"dashboard_password_scrypt_n": 1000},
        {"dashboard_password_min_length": 8},
        {"dashboard_session_idle_minutes": 0},
        {"dashboard_login_max_failures": 0},
    ],
)
def test_invalid_access_settings_are_rejected(values):
    with pytest.raises(ValueError):
        Settings(_env_file=None, **values)


def test_cookie_secure_auto_follows_app_env_and_defaults():
    settings = Settings(_env_file=None)
    assert settings.dashboard_cookie_secure_effective is False  # APP_ENV=dev
    assert Settings(_env_file=None, app_env="prod").dashboard_cookie_secure_effective is True
    assert Settings(_env_file=None, app_env="prod", dashboard_cookie_secure="false").dashboard_cookie_secure_effective is False
    assert (settings.dashboard_session_idle_minutes, settings.dashboard_session_max_hours) == (30, 8)
    assert (settings.dashboard_login_max_failures, settings.dashboard_login_lockout_minutes) == (5, 15)
    assert (settings.dashboard_password_scrypt_n, settings.dashboard_password_scrypt_r, settings.dashboard_password_scrypt_p) == (32768, 8, 3)
    assert settings.dashboard_legacy_credentials_set is False


# ---------------------------------------------------------------------------
# passwords (D12, D13)
# ---------------------------------------------------------------------------


def test_hash_format_verify_and_unique_salts():
    password = _pw()
    stored = passwords.hash_password(password, CHEAP_PARAMS)
    scheme, n, r, p, salt, digest = stored.split("$")
    assert (scheme, n, r, p) == ("scrypt", "16", "1", "1") and salt and digest
    assert password not in stored
    assert passwords.verify_password(password, stored)
    assert not passwords.verify_password(password + "x", stored)
    assert passwords.hash_password(password, CHEAP_PARAMS) != stored


def test_default_cost_hash_verifies():
    params = passwords.params_from_settings(Settings(_env_file=None))
    password = _pw()
    assert passwords.verify_password(password, passwords.hash_password(password, params))


@pytest.mark.parametrize("stored", ["", "plain", "scrypt$15$1$1$AAAA$AAAA", "bcrypt$16$1$1$AAAA$AAAA", "scrypt$16$1$1$***$AAAA"])
def test_malformed_hashes_never_verify(stored):
    assert passwords.verify_password("anything-at-all", stored) is False
    assert passwords.needs_rehash(stored, CHEAP_PARAMS) is True


def test_needs_rehash_and_dummy_verify():
    stored = passwords.hash_password(_pw(), CHEAP_PARAMS)
    assert passwords.needs_rehash(stored, CHEAP_PARAMS) is False
    assert passwords.needs_rehash(stored, passwords.ScryptParams(n=32, r=1, p=1)) is True
    assert passwords.dummy_verify(_pw(), CHEAP_PARAMS) is False
    assert asyncio.run(passwords.verify_password_async("x" * 2000, stored)) is False  # over-long input


@pytest.mark.parametrize(
    ("password", "problem"),
    [
        ("short-pw", "password_too_short"),
        ("alice.wonder1", "password_equals_username"),
        ("Password1234", "password_too_common"),
        ("aaaaaaaaaaaaaa", "password_too_common"),
        ("ab" * 7, "password_too_common"),
    ],
)
def test_password_rules_reject(password, problem):
    assert problem in passwords.password_problems(password, username="alice.wonder1", min_length=12)


@pytest.mark.parametrize("password", ["correct horse battery", "plain lowercase words ok", "長度足夠的中文密碼句子喔", "Tr0ub4dor&3xyz"])
def test_password_rules_accept_without_composition_rules(password):
    assert passwords.password_problems(password, username="alice", min_length=12) == []


# ---------------------------------------------------------------------------
# users (D11, D13, D14, D18, D22)
# ---------------------------------------------------------------------------


def test_create_user_hashes_password_and_writes_event(session_factory):
    settings = _settings()
    password = _pw()

    async def scenario(session):
        user = await _create(session, settings, username=" Alice ", password=password)
        events = list((await session.scalars(select(GovernanceEvent))).all())
        return user, events

    user, events = _db(session_factory, scenario)
    assert user.user_id.startswith("usr_") and user.username == "alice"
    assert user.password_hash.startswith("scrypt$") and password not in user.password_hash
    assert user.must_change_password is True and user.status == "active" and user.auth_source == "local"
    assert [event.event_type for event in events] == ["access.user_created"]
    assert events[0].tenant_id == "system" and events[0].user_id == "test"
    assert password not in json.dumps(events[0].details) + events[0].reason
    assert "password_hash" not in users.user_view(user)


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"username": "x"}, "invalid_username"),
        ({"username": "bad name"}, "invalid_username"),
        ({"role": "superuser"}, "invalid_role"),
        ({"role": "tenant_admin"}, "tenant_required_for_role"),
        ({"role": "admin", "tenant_id": "t1"}, "role_is_platform_only"),
        ({"password": "admin"}, "password_too_short"),
    ],
)
def test_create_user_refusals(session_factory, kwargs, code):
    settings = _settings()

    async def scenario(session):
        base = {"username": "bob", "password": _pw(), "role": "reviewer", "tenant_id": None, **kwargs}
        await users.create_user(session, settings, actor="test", **base)

    with pytest.raises(AccessError) as excinfo:
        _db(session_factory, scenario)
    assert excinfo.value.code == code


def test_duplicate_username_and_tenant_bound_roles(session_factory):
    settings = _settings()

    async def scenario(session):
        await _create(session, settings, username="carol", role="tenant_admin", tenant_id="t-1")
        auditor = await _create(session, settings, username="dave", role="auditor")
        with pytest.raises(AccessError) as excinfo:
            await _create(session, settings, username="CAROL", role="reviewer")
        return auditor, excinfo.value

    auditor, error = _db(session_factory, scenario)
    assert auditor.tenant_id is None and error.code == "username_taken" and error.status_code == 409


def test_last_admin_cannot_be_disabled_or_demoted_and_no_self_changes(session_factory):
    settings = _settings()

    async def scenario(session):
        admin = await _create(session, settings, username="root")
        codes = []
        for call in (
            users.set_status(session, user_id=admin.user_id, status="disabled", actor="cli"),
            users.change_role(session, user_id=admin.user_id, role="auditor", tenant_id=None, actor="cli"),
        ):
            with pytest.raises(AccessError) as excinfo:
                await call
            codes.append(excinfo.value.code)
        second = await _create(session, settings, username="root2")
        with pytest.raises(AccessError) as own_role:
            await users.change_role(session, user_id=second.user_id, role="auditor", tenant_id=None, actor="u", actor_user_id=second.user_id)
        with pytest.raises(AccessError) as own_disable:
            await users.set_status(session, user_id=second.user_id, status="disabled", actor="u", actor_user_id=second.user_id)
        demoted = await users.change_role(session, user_id=admin.user_id, role="auditor", tenant_id=None, actor="cli")
        await session.commit()
        return codes, own_role.value.code, own_disable.value.code, demoted.role, await users.count_active_admins(session)

    codes, own_role, own_disable, role, admins = _db(session_factory, scenario)
    assert codes == ["last_admin_protected", "last_admin_protected"]
    assert (own_role, own_disable) == ("cannot_change_own_role", "cannot_disable_self")
    assert role == "auditor" and admins == 1


def test_disable_role_change_and_password_change_revoke_sessions(session_factory):
    settings = _settings()

    async def scenario(session):
        await _create(session, settings, username="admin1")
        user = await _create(session, settings, username="erin", role="operator")
        results = {}
        for label, action in (
            ("role_changed", lambda: users.change_role(session, user_id=user.user_id, role="reviewer", tenant_id=None, actor="cli")),
            ("password_changed", lambda: users.set_password(session, settings, user_id=user.user_id, new_password=_pw(), actor="cli", must_change_password=True)),
            ("user_disabled", lambda: users.set_status(session, user_id=user.user_id, status="disabled", actor="cli")),
        ):
            token, _ = await sessions.create_session(session, settings, user, auth_method="local_password")
            await action()
            await session.commit()
            row = await session.scalar(select(DashboardSession).where(DashboardSession.session_hash == sessions.token_hash(token)))
            results[label] = row.revoked_reason
        types = [event.event_type for event in (await session.scalars(select(GovernanceEvent).order_by(GovernanceEvent.id))).all()]
        return results, types

    results, types = _db(session_factory, scenario)
    assert results == {"role_changed": "role_changed", "password_changed": "password_changed", "user_disabled": "user_disabled"}
    assert types[-3:] == ["access.role_changed", "access.password_changed", "access.user_disabled"]


def test_set_password_rejects_reuse_and_weak_values(session_factory):
    settings = _settings()
    password = _pw()

    async def scenario(session):
        user = await _create(session, settings, username="frank", role="reviewer", password=password)
        codes = []
        for candidate in (password, "frank", "short"):
            with pytest.raises(AccessError) as excinfo:
                await users.set_password(session, settings, user_id=user.user_id, new_password=candidate, actor="u", must_change_password=False)
            codes.append(excinfo.value.code)
        return codes

    assert _db(session_factory, scenario) == ["password_unchanged", "password_too_short", "password_too_short"]


# ---------------------------------------------------------------------------
# sign-in provider and lockout (D16, D17)
# ---------------------------------------------------------------------------


def test_local_provider_success_resets_failures_and_resolves_user(session_factory):
    settings = _settings()
    password = _pw()
    provider = providers.get_provider(settings, "local")

    async def scenario(session):
        user = await _create(session, settings, username="gina", role="reviewer", password=password)
        await provider.authenticate(session, settings, {"username": "gina", "password": "wrong-" + password})
        result = await provider.authenticate(session, settings, {"username": " GINA ", "password": password})
        resolved = await providers.resolve_user(session, result.identity)
        return user, result, resolved

    user, result, resolved = _db(session_factory, scenario)
    assert result.ok and result.reason == "ok" and result.identity == Identity("local", user.user_id, "gina", "")
    assert resolved.user_id == user.user_id and resolved.failed_attempts == 0 and resolved.last_login_at is not None


def test_lockout_after_max_failures_refuses_the_right_password(session_factory):
    settings = _settings()
    password = _pw()
    provider = providers.LocalPasswordProvider()

    async def scenario(session):
        user = await _create(session, settings, username="hank", role="operator", password=password)
        results = [await provider.authenticate(session, settings, {"username": "hank", "password": f"nope-{i}"}) for i in range(5)]
        during = await provider.authenticate(session, settings, {"username": "hank", "password": password})
        await session.commit()
        lock_events = list((await session.scalars(select(GovernanceEvent).where(GovernanceEvent.event_type == "access.lockout"))).all())
        user.locked_until = datetime.now(timezone.utc) - timedelta(seconds=1)  # the lock expired
        after = await provider.authenticate(session, settings, {"username": "hank", "password": password})
        return results, during, lock_events, after

    results, during, lock_events, after = _db(session_factory, scenario)
    assert [result.reason for result in results] == ["wrong_password"] * 4 + ["lockout"]
    assert results[-1].locked_now and during.reason == "locked" and during.error == "invalid_credentials"
    assert len(lock_events) == 1 and lock_events[0].decision == "deny" and password not in json.dumps(lock_events[0].details)
    assert after.ok


def test_unknown_disabled_and_wrong_password_look_identical(session_factory):
    settings = _settings()
    password = _pw()
    provider = providers.LocalPasswordProvider()

    async def scenario(session):
        await _create(session, settings, username="admin1")
        user = await _create(session, settings, username="ivy", role="reviewer", password=password)
        await users.set_status(session, user_id=user.user_id, status="disabled", actor="cli")
        await session.commit()
        return [
            await provider.authenticate(session, settings, {"username": "nobody", "password": password}),
            await provider.authenticate(session, settings, {"username": "ivy", "password": password}),
            await provider.authenticate(session, settings, {"username": "admin1", "password": password}),
        ]

    outcomes = _db(session_factory, scenario)
    assert {(result.identity, result.error) for result in outcomes} == {(None, "invalid_credentials")}
    assert [result.reason for result in outcomes] == ["unknown_user", "disabled", "wrong_password"]


def test_successful_login_rehashes_a_weaker_stored_hash(session_factory):
    weak, strong = _settings(), _settings(dashboard_password_scrypt_n=32)
    password = _pw()

    async def scenario(session):
        user = await _create(session, weak, username="jack", role="reviewer", password=password)
        result = await providers.LocalPasswordProvider().authenticate(session, strong, {"username": "jack", "password": password})
        return result, user.password_hash

    result, stored = _db(session_factory, scenario)
    assert result.ok and stored.startswith("scrypt$32$1$1$") and passwords.verify_password(password, stored)


def test_reauthenticate_counts_failures_towards_lockout(session_factory):
    settings = _settings(dashboard_login_max_failures=2)
    password = _pw()
    provider = providers.LocalPasswordProvider()

    async def scenario(session):
        user = await _create(session, settings, username="kate", role="reviewer", password=password)
        ok = await provider.reauthenticate(session, settings, user, {"password": password})
        first = await provider.reauthenticate(session, settings, user, {"password": "bad-1"})
        second = await provider.reauthenticate(session, settings, user, {"password": "bad-2"})
        return ok, first, second, users.is_locked(user)

    ok, first, second, locked = _db(session_factory, scenario)
    assert ok.ok and first.reason == "wrong_password" and second.reason == "lockout" and locked


def test_provider_registry_satisfies_the_protocol():
    registry = providers.get_providers(_settings())
    assert [provider.name for provider in registry] == ["local"]
    assert isinstance(registry[0], providers.AuthProvider) and registry[0].kind == "password"
    assert providers.get_provider(_settings(), "oidc") is None


# ---------------------------------------------------------------------------
# sessions (D15)
# ---------------------------------------------------------------------------


def test_session_stores_only_the_token_hash_and_resolves(session_factory):
    settings = _settings()

    async def scenario(session):
        user = await _create(session, settings, username="liam", role="auditor")
        token, row = await sessions.create_session(session, settings, user, auth_method="local_password", user_agent="pytest")
        await session.commit()
        lookup = await sessions.resolve_session(session, settings, token)
        return token, row, lookup

    token, row, lookup = _db(session_factory, scenario)
    assert len(token) >= 43 and row.session_hash == sessions.token_hash(token) and token not in (row.session_hash, row.csrf_secret)
    assert row.session_id.startswith("ses_") and len(row.csrf_secret) == 64 and row.user_agent_hash
    assert lookup.user.username == "liam" and lookup.touched is False
    assert sessions.COOKIE_NAME == "rh_session" and sessions.COOKIE_PATH == "/dashboard"


def test_idle_absolute_revoked_and_disabled_sessions_stop_resolving(session_factory):
    settings = _settings()

    async def scenario(session):
        await _create(session, settings, username="admin1")
        user = await _create(session, settings, username="mia", role="operator")
        now = datetime.now(timezone.utc)
        idle_token, _ = await sessions.create_session(session, settings, user, auth_method="local_password", now=now - timedelta(minutes=31))
        old_token, old = await sessions.create_session(session, settings, user, auth_method="local_password", now=now - timedelta(hours=9))
        old.last_seen_at = now  # active, but past the absolute timeout
        revoked_token, revoked = await sessions.create_session(session, settings, user, auth_method="local_password")
        await sessions.revoke_session(session, revoked, reason="logout")
        live_token, _ = await sessions.create_session(session, settings, user, auth_method="local_password")
        await session.commit()
        results = {
            "idle": await sessions.resolve_session(session, settings, idle_token),
            "absolute": await sessions.resolve_session(session, settings, old_token),
            "revoked": await sessions.resolve_session(session, settings, revoked_token),
            "unknown": await sessions.resolve_session(session, settings, "not-a-token"),
            "empty": await sessions.resolve_session(session, settings, ""),
            "live": await sessions.resolve_session(session, settings, live_token),
        }
        await users.set_status(session, user_id=user.user_id, status="disabled", actor="cli")
        results["after_disable"] = await sessions.resolve_session(session, settings, live_token)
        reasons = {row.revoked_reason for row in (await session.scalars(select(DashboardSession))).all()}
        return results, reasons

    results, reasons = _db(session_factory, scenario)
    assert results["live"] is not None
    assert all(results[key] is None for key in ("idle", "absolute", "revoked", "unknown", "empty", "after_disable"))
    assert {"idle_timeout", "absolute_timeout", "logout", "user_disabled"} <= reasons


def test_last_seen_is_written_at_most_every_minute_and_reauth_age(session_factory):
    settings = _settings()

    async def scenario(session):
        user = await _create(session, settings, username="noah", role="reviewer")
        now = datetime.now(timezone.utc)
        token, row = await sessions.create_session(session, settings, user, auth_method="local_password", now=now - timedelta(seconds=30))
        quick = await sessions.resolve_session(session, settings, token, now=now)
        later = await sessions.resolve_session(session, settings, token, now=now + timedelta(seconds=45))
        fresh = sessions.reauth_is_fresh(row.reauth_at, settings, now=now)
        stale = sessions.reauth_is_fresh(row.reauth_at, settings, now=now + timedelta(seconds=300))
        await sessions.mark_reauthenticated(session, row, now=now + timedelta(seconds=300))
        renewed = sessions.reauth_is_fresh(row.reauth_at, settings, now=now + timedelta(seconds=301))
        return quick.touched, later.touched, fresh, stale, renewed

    assert _db(session_factory, scenario) == (False, True, True, False, True)


def test_revoke_user_sessions_can_keep_one(session_factory):
    settings = _settings()

    async def scenario(session):
        user = await _create(session, settings, username="olga", role="reviewer")
        tokens = [await sessions.create_session(session, settings, user, auth_method="local_password") for _ in range(3)]
        count = await sessions.revoke_user_sessions(session, user.user_id, reason="logout_all", except_session_id=tokens[0][1].session_id)
        await session.commit()
        live = await sessions.list_user_sessions(session, user.user_id)
        return count, [row.session_id for row in live], tokens[0][1].session_id

    count, live, kept = _db(session_factory, scenario)
    assert count == 2 and live == [kept]


# ---------------------------------------------------------------------------
# principal and permission table (D16, D19-D22)
# ---------------------------------------------------------------------------


def test_principal_from_user_and_session_and_local_admin(session_factory):
    settings = _settings()

    async def scenario(session):
        user = await _create(session, settings, username="pat", role="tenant_admin", tenant_id="t-9")
        _, row = await sessions.create_session(session, settings, user, auth_method="local_password")
        return principal_for(user, row), row

    principal, row = _db(session_factory, scenario)
    assert principal.actor == "user:pat" and principal.role == "tenant_admin" and principal.tenant_id == "t-9"
    assert principal.session_id == row.session_id and principal.must_change_password and not principal.is_platform
    assert LOCAL_ADMIN.role == "admin" and LOCAL_ADMIN.synthetic and LOCAL_ADMIN.is_platform and LOCAL_ADMIN.session_id is None


@pytest.mark.parametrize(
    ("role", "allowed", "denied"),
    [
        ("admin", {"users.manage", "retention.apply", "legal_hold.release", "audit_chain.verify", "pricing.manage"}, set()),
        ("tenant_admin", {"finops.manage", "reviews.decide", "legal_hold.create", "change_requests.decide"},
         {"users.manage", "retention.apply", "pricing.manage", "control_plane.manage", "audit_chain.read"}),
        ("reviewer", {"reviews.decide", "evaluations.preview", "dashboard.view"}, {"runs.submit", "outcomes.submit", "worker.manage"}),
        # P3 (D20): breakers are admin only, as in the API
        ("operator", {"runs.submit", "outcomes.submit", "worker.manage"}, {"reviews.decide", "judge.profile.write", "breakers.manage"}),
        ("auditor", {"dashboard.view", "account.self", "audit_chain.read", "audit_chain.verify"}, {"approvals.decide", "audit_chain.checkpoint"}),
    ],
)
def test_permission_matrix(role, allowed, denied):
    assert all(permissions.has_permission(role, name) for name in allowed)
    assert not any(permissions.has_permission(role, name) for name in denied)


def test_auditor_holds_no_write_permission_and_table_is_consistent():
    # P3 added read-only view permissions (benchmark.view, judge.view, retention.view, finops.export, api_keys.view)
    assert permissions.permissions_for("auditor") == permissions.READ_PERMISSIONS
    assert permissions.STEP_UP_PERMISSIONS <= set(permissions.PERMISSIONS)
    assert all(roles <= set(permissions.ROLES) for roles in permissions.PERMISSIONS.values())
    assert permissions.has_permission("admin", "no.such.permission") is False
    assert permissions.has_permission(None, "dashboard.view") is False
    assert permissions.requires_step_up("retention.apply") and not permissions.requires_step_up("dashboard.view")
    assert "operations" not in permissions.TENANT_VIEW_ALLOWLIST and "runs" in permissions.TENANT_VIEW_ALLOWLIST


@pytest.mark.parametrize(
    ("role", "tenant_id", "problem"),
    [("admin", None, None), ("admin", "t", "role_is_platform_only"), ("tenant_admin", None, "tenant_required_for_role"),
     ("tenant_admin", "t", None), ("reviewer", None, None), ("auditor", "t", None), ("root", None, "invalid_role")],
)
def test_tenant_rule(role, tenant_id, problem):
    assert permissions.tenant_rule_problem(role, tenant_id) == problem


# ---------------------------------------------------------------------------
# CLI (D14)
# ---------------------------------------------------------------------------


def _cli(session_factory, argv, settings, capsys, stdin=""):
    code = users_cli.main(argv, settings=settings, session_factory=session_factory, stdin=io.StringIO(stdin))
    out = capsys.readouterr().out
    return code, json.loads(out), out


def test_cli_creates_first_admin_from_env_without_echoing_the_password(session_factory, capsys, monkeypatch):
    settings = _settings()
    password = _pw()
    monkeypatch.setenv("RH_TEST_NEW_PASSWORD", password)
    code, payload, raw = _cli(session_factory, ["create", "--username", "Root", "--role", "admin", "--password-env", "RH_TEST_NEW_PASSWORD"], settings, capsys)
    assert code == 0 and payload["user"]["username"] == "root" and payload["user"]["must_change_password"] is True
    assert password not in raw and "scrypt$" not in raw
    events = _db(session_factory, lambda s: s.scalars(select(GovernanceEvent)))
    assert all(password not in json.dumps(event.details) for event in events.all())


def test_cli_reads_stdin_and_manages_users(session_factory, capsys):
    settings = _settings()
    password, other = _pw(), _pw()
    assert _cli(session_factory, ["create", "--username", "root", "--role", "admin"], settings, capsys, stdin=password + "\n")[0] == 0
    code, payload, _ = _cli(session_factory, ["create", "--username", "rita", "--role", "tenant_admin", "--tenant-id", "t-7"], settings, capsys, stdin=other + "\r\n")
    assert code == 0 and payload["user"]["tenant_id"] == "t-7"
    assert _cli(session_factory, ["set-role", "--username", "rita", "--role", "reviewer"], settings, capsys)[1]["user"]["role"] == "reviewer"
    assert _cli(session_factory, ["reset-password", "--username", "rita"], settings, capsys, stdin=_pw() + "\n")[0] == 0
    assert _cli(session_factory, ["disable", "--username", "rita"], settings, capsys)[1]["user"]["status"] == "disabled"
    assert _cli(session_factory, ["enable", "--username", "rita"], settings, capsys)[1]["user"]["status"] == "active"
    assert _cli(session_factory, ["unlock", "--username", "rita"], settings, capsys)[0] == 0
    code, listing, raw = _cli(session_factory, ["list"], settings, capsys)
    assert code == 0 and [user["username"] for user in listing["users"]] == ["rita", "root"] and "scrypt$" not in raw


@pytest.mark.parametrize(
    ("argv", "stdin", "code"),
    [
        (["create", "--username", "sam", "--role", "admin", "--password-env", "RH_TEST_UNSET_VARIABLE"], "", "password_env_missing"),
        (["create", "--username", "sam", "--role", "admin"], "", "password_missing"),
        (["create", "--username", "sam", "--role", "admin"], "sam\n", "password_too_short"),
        (["disable", "--username", "ghost"], "", "user_not_found"),
    ],
)
def test_cli_refusals_exit_2(session_factory, capsys, monkeypatch, argv, stdin, code):
    monkeypatch.delenv("RH_TEST_UNSET_VARIABLE", raising=False)
    exit_code, payload, _ = _cli(session_factory, argv, _settings(), capsys, stdin=stdin)
    assert exit_code == 2 and payload["error"]["code"] == code


def test_cli_refuses_to_disable_the_last_admin(session_factory, capsys):
    settings = _settings()
    _cli(session_factory, ["create", "--username", "root", "--role", "admin"], settings, capsys, stdin=_pw() + "\n")
    code, payload, _ = _cli(session_factory, ["disable", "--username", "root"], settings, capsys)
    assert code == 2 and payload["error"]["code"] == "last_admin_protected"


def test_cli_rejects_password_arguments():
    with pytest.raises(SystemExit):
        users_cli.build_parser().parse_args(["create", "--username", "a", "--role", "admin", "--password", "x"])
