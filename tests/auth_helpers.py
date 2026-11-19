"""Test helpers for database API keys (v1.6.3).

Since v1.6.3 environment keys (API_KEYS) are refused outside APP_ENV=dev / test, so production-like tests authenticate
with database keys seeded directly into the test database.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.models.tenant import Tenant, TenantApiKey
from app.services.auth import hash_api_key
from app.services.governance_audit import add_governance_event


def seed_db_api_key(
    session_factory,
    plaintext: str,
    *,
    tenant_id: str = "default",
    role: str = "admin",
    key_id: str | None = None,
    tenant_status: str = "active",
    created_by: str | None = None,
) -> str:
    """Create the tenant (when missing) and an active database key whose plaintext is ``plaintext``; return its key_id.

    ``created_by`` also writes the ``api_key.created`` event with that actor (``"cli"`` = the v1.6.3 CLI), so the key
    has a recorded creator; without it the key counts in the ``unattributed_api_keys`` check (D20)."""
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
            if created_by is not None:
                add_governance_event(
                    session,
                    tenant_id=tenant_id,
                    user_id=created_by,
                    event_type="api_key.created",
                    decision="created",
                    reason=f"API key {key_id} ({role}) created from the CLI.",
                    details={"key_id": key_id, "role": role, "key_prefix": plaintext[:8]},
                )
            await session.commit()

    asyncio.run(scenario())
    return key_id
