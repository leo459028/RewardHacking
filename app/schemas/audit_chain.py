"""v1.6.1 audit chain API request bodies (plan D35)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class AuditChainVerifyRequest(BaseModel):
    """Optional range for ``POST /v1/audit-chain/verify``; the server caps it at ``AUDIT_CHAIN_VERIFY_MAX_ENTRIES``."""

    model_config = ConfigDict(extra="forbid")

    from_seq: int | None = Field(default=None, ge=1)
    to_seq: int | None = Field(default=None, ge=1)
