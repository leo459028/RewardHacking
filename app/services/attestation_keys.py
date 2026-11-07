"""v1.5.9 tenant attestation trust roots (admin-registered public keys, revocable)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.benchmark import TenantAttestationKey
from app.models.governance_event import GovernanceEvent
from app.services.evaluation.attestation import AttestationKeyError, TrustedKey, key_fingerprint, load_public_key


class AttestationKeyServiceError(ValueError):
    def __init__(self, code: str, detail: str, status_code: int = 422):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status_code = status_code


def new_attestation_key_id() -> str:
    return f"attk_{uuid4().hex[:24]}"


async def list_attestation_keys(session: AsyncSession, tenant_id: str) -> list[TenantAttestationKey]:
    return list(
        (
            await session.scalars(
                select(TenantAttestationKey)
                .where(TenantAttestationKey.tenant_id == tenant_id)
                .order_by(TenantAttestationKey.created_at.desc(), TenantAttestationKey.id.desc())
            )
        ).all()
    )


async def trusted_keys_for(session: AsyncSession, tenant_id: str) -> list[TrustedKey]:
    return [
        TrustedKey(
            key_id=row.key_id,
            algorithm=row.algorithm,
            public_key_pem=row.public_key_pem,
            fingerprint=row.key_fingerprint,
            status=row.status,
            allowed_subjects=list(row.allowed_subjects or []),
            name=row.name,
        )
        for row in await list_attestation_keys(session, tenant_id)
    ]


async def create_attestation_key(
    session: AsyncSession,
    *,
    tenant_id: str,
    name: str,
    algorithm: str,
    public_key_pem: str,
    allowed_subjects: list[str] | None,
    created_by: str,
) -> TenantAttestationKey:
    pem = (public_key_pem or "").strip()
    if "PRIVATE KEY" in pem:
        raise AttestationKeyServiceError("attestation_key_invalid", "Register a public key only; private keys are never accepted.")
    try:
        load_public_key(pem, algorithm)
        fingerprint = key_fingerprint(pem)
    except AttestationKeyError as exc:
        raise AttestationKeyServiceError("attestation_key_invalid", str(exc)) from exc
    except (ValueError, TypeError) as exc:
        raise AttestationKeyServiceError("attestation_key_invalid", "public_key_pem is not a valid PEM public key.") from exc
    duplicate = await session.scalar(
        select(TenantAttestationKey).where(
            TenantAttestationKey.tenant_id == tenant_id,
            TenantAttestationKey.key_fingerprint == fingerprint,
            TenantAttestationKey.status == "active",
        )
    )
    if duplicate is not None:
        raise AttestationKeyServiceError("attestation_key_duplicate", f"This key is already registered as {duplicate.key_id}.", 409)
    row = TenantAttestationKey(
        key_id=new_attestation_key_id(),
        tenant_id=tenant_id,
        name=(name or "").strip()[:128],
        algorithm=algorithm,
        public_key_pem=pem + "\n",
        key_fingerprint=fingerprint,
        allowed_subjects=[str(item).strip() for item in (allowed_subjects or []) if str(item).strip()][:50],
        status="active",
        created_by=created_by[:128],
    )
    session.add(row)
    session.add(
        GovernanceEvent(
            tenant_id=tenant_id,
            user_id=created_by[:128],
            event_type="evaluation.attestation_key.created",
            decision="created",
            reason=f"Attestation key {row.key_id} registered",
            details={"key_id": row.key_id, "fingerprint": fingerprint, "algorithm": algorithm},
        )
    )
    await session.commit()
    await session.refresh(row)
    return row


async def revoke_attestation_key(session: AsyncSession, *, tenant_id: str, key_id: str, actor: str) -> TenantAttestationKey:
    row = await session.scalar(
        select(TenantAttestationKey).where(TenantAttestationKey.tenant_id == tenant_id, TenantAttestationKey.key_id == key_id)
    )
    if row is None:
        raise AttestationKeyServiceError("attestation_key_not_found", f"Attestation key {key_id} not found.", 404)
    if row.status != "revoked":
        row.status = "revoked"
        row.revoked_at = datetime.now(timezone.utc)
        session.add(
            GovernanceEvent(
                tenant_id=tenant_id,
                user_id=actor[:128],
                event_type="evaluation.attestation_key.revoked",
                decision="revoked",
                reason=f"Attestation key {key_id} revoked",
                details={"key_id": key_id},
            )
        )
        await session.commit()
        await session.refresh(row)
    return row


def key_to_dict(row: TenantAttestationKey) -> dict[str, Any]:
    return {
        "key_id": row.key_id,
        "tenant_id": row.tenant_id,
        "name": row.name,
        "algorithm": row.algorithm,
        "key_fingerprint": row.key_fingerprint,
        "public_key_pem": row.public_key_pem,
        "allowed_subjects": list(row.allowed_subjects or []),
        "status": row.status,
        "created_by": row.created_by,
        "created_at": row.created_at,
        "revoked_at": row.revoked_at,
    }
