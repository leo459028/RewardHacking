from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.infra.postgres import get_session
from app.models.tenant import Tenant, TenantApiKey


@dataclass(frozen=True)
class AuthContext:
    enabled: bool
    tenant_id: str | None = None
    role: str = "admin"
    key_id: str = ""
    source: str = "disabled"


def _api_key_map(raw: str) -> dict[str, tuple[str, str]]:
    mapping: dict[str, tuple[str, str]] = {}
    for item in raw.split(","):
        if ":" not in item:
            continue
        parts = [part.strip() for part in item.split(":")]
        key = parts[0]
        tenant_id = parts[1] if len(parts) > 1 else ""
        role = parts[2] if len(parts) > 2 and parts[2] else "admin"
        if key and tenant_id:
            mapping[key] = (tenant_id, role)
    return mapping


def auth_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message},
    )


def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def generate_api_key() -> str:
    return f"rg_{secrets.token_urlsafe(24)}"


def require_role(auth_context: AuthContext, *roles: str) -> None:
    if not auth_context.enabled:
        return
    if auth_context.role not in roles:
        raise auth_error(403, "insufficient_role", "API key role is not allowed for this operation.")


async def get_auth_context(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    session: AsyncSession = Depends(get_session),
) -> AuthContext:
    settings = get_settings()
    if not settings.auth_enabled:
        return AuthContext(enabled=False)

    if not x_api_key:
        raise auth_error(401, "auth_required", "X-API-Key header is required.")

    legacy_key = _api_key_map(settings.api_keys).get(x_api_key)
    if legacy_key is not None:
        tenant_id, role = legacy_key
        return AuthContext(
            enabled=True,
            tenant_id=tenant_id,
            role=role,
            key_id="legacy",
            source="env",
        )

    api_key = await session.scalar(
        select(TenantApiKey).where(TenantApiKey.key_hash == hash_api_key(x_api_key))
    )
    if api_key is None or api_key.revoked or api_key.status != "active":
        raise auth_error(401, "invalid_api_key", "X-API-Key is invalid.")

    tenant = await session.scalar(select(Tenant).where(Tenant.tenant_id == api_key.tenant_id))
    if tenant is None or tenant.status != "active":
        raise auth_error(403, "tenant_inactive", "Tenant is inactive or missing.")

    return AuthContext(
        enabled=True,
        tenant_id=api_key.tenant_id,
        role=api_key.role,
        key_id=api_key.key_id,
        source="db",
    )
