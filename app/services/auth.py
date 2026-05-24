from __future__ import annotations

from dataclasses import dataclass

from fastapi import Header, HTTPException

from app.core.config import get_settings


@dataclass(frozen=True)
class AuthContext:
    enabled: bool
    tenant_id: str | None = None


def _api_key_map(raw: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for item in raw.split(","):
        if ":" not in item:
            continue
        key, tenant_id = item.split(":", 1)
        key = key.strip()
        tenant_id = tenant_id.strip()
        if key and tenant_id:
            mapping[key] = tenant_id
    return mapping


def auth_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message},
    )


async def get_auth_context(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> AuthContext:
    settings = get_settings()
    if not settings.auth_enabled:
        return AuthContext(enabled=False)

    if not x_api_key:
        raise auth_error(401, "auth_required", "X-API-Key header is required.")

    tenant_id = _api_key_map(settings.api_keys).get(x_api_key)
    if tenant_id is None:
        raise auth_error(401, "invalid_api_key", "X-API-Key is invalid.")

    return AuthContext(enabled=True, tenant_id=tenant_id)
