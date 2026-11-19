"""Test helpers for database API keys (v1.6.3).

Since v1.6.3 environment keys (API_KEYS) are refused outside APP_ENV=dev / test, so production-like tests authenticate
with database keys seeded directly into the test database.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.models.tenant import Tenant, TenantApiKey
from app.services.auth import hash_api_key


def seed_db_api_key(
    session_factory,
    plaintext: str,
    *,
    tenant_id: str = "default",
    role: str = "admin",
    key_id: str | None = None,
    tenant_status: str = "active",
) -> str:
    """Create the tenant (when missing) and an active database key whose plaintext is ``plaintext``; return its key_id."""
    key_id = key_id or f"key_test_{hash_api_key(plaintext)[:12]}"

    async def scenario() -> None:
        async with session_factory() as session:
            tenant = await session.scalar(select(Tenant).where(Tenant.tenant_id == tenant_id))
            if tenant is None:
                session.add(Tenant(tenant_id=tenant_id, display_name=f"{tenant_id} tenant", status=tenant_status))
            session.add(
                TenantApiKey(
                    key_id=key_id,
                    tenant_id=tenant_id,
                    name=f"test {role} key",
                    key_prefix=plaintext[:8],
                    key_hash=hash_api_key(plaintext),
                    role=role,
                    status="active",
                    revoked=False,
                )
            )
            await session.commit()

    asyncio.run(scenario())
    return key_id
