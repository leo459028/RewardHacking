"""Sign-in providers (v1.6.1 plan D16, D17).

Sign-in is three steps so a later OIDC provider only adds a provider, a callback route and a group-to-role mapping:
``provider.authenticate()`` -> ``Identity``; ``resolve_user(identity)`` -> ``DashboardUser``;
``sessions.create_session(user, auth_method)``. Re-authentication goes through ``provider.reauthenticate()``
(local: type the password again; OIDC later: ``max_age`` / ``prompt=login``).

``LocalPasswordProvider`` is the only provider in this release. Account enumeration (D17): an unknown username,
a disabled account, a locked account and a wrong password all return the same ``invalid_credentials`` result and
spend the same scrypt work; the internal ``reason`` is for metrics only and must not reach the response.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Protocol, runtime_checkable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.access import DashboardUser
from app.services.access import passwords, users
from app.services.access.errors import utcnow
from app.services.access.principal import Identity

INVALID_CREDENTIALS = "invalid_credentials"


@dataclass(frozen=True)
class AuthResult:
    identity: Identity | None
    error: str | None = None  # the only value a response may show: "invalid_credentials"
    reason: str = ""  # internal (metrics): ok | unknown_user | disabled | locked | wrong_password | lockout
    locked_now: bool = False

    @property
    def ok(self) -> bool:
        return self.identity is not None


@runtime_checkable
class AuthProvider(Protocol):
    name: str
    label: str
    kind: str  # "password" renders username / password fields; a redirect provider would render a button

    async def authenticate(self, session: AsyncSession, settings: Settings, credentials: Mapping[str, str]) -> AuthResult: ...

    async def reauthenticate(
        self, session: AsyncSession, settings: Settings, user: DashboardUser, credentials: Mapping[str, str]
    ) -> AuthResult: ...


def _identity(user: DashboardUser, provider: str) -> Identity:
    return Identity(provider=provider, subject=user.user_id, username=user.username, display_name=user.display_name or "")


class LocalPasswordProvider:
    name = "local"
    label = "Username and password | 帳號密碼"
    kind = "password"
    auth_method = "local_password"

    async def _check(
        self, session: AsyncSession, settings: Settings, user: DashboardUser | None, password: str, now: datetime
    ) -> AuthResult:
        params = passwords.params_from_settings(settings)
        if user is None:
            await passwords.dummy_verify_async(password, params)
            return AuthResult(None, INVALID_CREDENTIALS, "unknown_user")
        matches = await passwords.verify_password_async(password, user.password_hash)
        if user.status != "active":
            return AuthResult(None, INVALID_CREDENTIALS, "disabled")
        if users.is_locked(user, now=now):
            return AuthResult(None, INVALID_CREDENTIALS, "locked")  # D17: even the right password is refused
        if not matches:
            locked_now = await users.record_failed_login(session, settings, user, now=now)
            return AuthResult(None, INVALID_CREDENTIALS, "lockout" if locked_now else "wrong_password", locked_now=locked_now)
        if passwords.needs_rehash(user.password_hash, params):
            user.password_hash = await passwords.hash_password_async(password, params)
        await users.record_successful_login(session, user, now=now)
        return AuthResult(_identity(user, self.name), None, "ok")

    async def authenticate(self, session: AsyncSession, settings: Settings, credentials: Mapping[str, str]) -> AuthResult:
        raw_username = (credentials.get("username") or "").strip().lower()
        password = credentials.get("password") or ""
        user = None
        if raw_username and len(raw_username) <= 64:
            user = await session.scalar(
                select(DashboardUser).where(DashboardUser.username == raw_username, DashboardUser.auth_source == "local")
            )
        return await self._check(session, settings, user, password, utcnow())

    async def reauthenticate(
        self, session: AsyncSession, settings: Settings, user: DashboardUser, credentials: Mapping[str, str]
    ) -> AuthResult:
        """Wrong passwords here count towards the lockout like sign-in failures (feature seven)."""
        return await self._check(session, settings, user, credentials.get("password") or "", utcnow())


_LOCAL = LocalPasswordProvider()


def get_providers(settings: Settings) -> list[AuthProvider]:
    """Providers rendered on the sign-in page, in order. Only the local provider exists in this release."""
    return [_LOCAL]


def get_provider(settings: Settings, name: str) -> AuthProvider | None:
    return next((provider for provider in get_providers(settings) if provider.name == name), None)


async def resolve_user(session: AsyncSession, identity: Identity) -> DashboardUser | None:
    """Map a provider identity to an active Dashboard user (local: ``subject`` is the ``user_id``)."""
    if identity.provider == LocalPasswordProvider.name:
        stmt = select(DashboardUser).where(DashboardUser.user_id == identity.subject)
    else:
        stmt = select(DashboardUser).where(
            DashboardUser.auth_source == identity.provider, DashboardUser.external_subject == identity.subject
        )
    user = await session.scalar(stmt)
    return user if user is not None and user.status == "active" else None
